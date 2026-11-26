"""Fresh, fail-closed lifecycle proofs and operator-only backup receipts."""

from dataclasses import asdict
import http.client
import json
import os
import ssl

import pytest

from authority_tls import approved_test_policy, custody, tls_authority


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Independent Unix custodian")
BINDING = ("prod-fixture-custody", "c" * 64, custody.CUTOFF)
RECEIPT = "a" * 64
OBJECT = "b" * 64


@pytest.fixture
def record(tmp_path):
    now = [custody.CUTOFF - 1]
    folder = tmp_path.resolve() / "custody"
    folder.mkdir(mode=0o700)
    ledger = custody.CustodyLedger.create(
        folder / "ledger.sqlite", *BINDING, approved_test_policy(), clock=lambda: now[0]
    )
    return ledger, now


def test_independent_time_latches_after_restart_and_rollback(record):
    ledger, now = record
    assert not ledger.status(*BINDING).closed
    # A live host's clock has moved forward. Only access is denied early.
    assert ledger.close(*BINDING).closed
    assert not ledger.status(*BINDING).purge_authorized
    with pytest.raises(custody.AuthorityError):
        ledger.record_purge(*BINDING)
    now[0] = custody.CUTOFF
    assert ledger.status(*BINDING).purge_authorized
    now[0] = custody.CUTOFF - 120
    restarted = custody.CustodyLedger(ledger.path, clock=lambda: now[0])
    assert restarted.record_purge(*BINDING).purged
    assert not restarted.status(*BINDING).backups_expired


def test_refused_first_request_commits_cutoff_and_event_binding(record):
    ledger, now = record
    now[0] = custody.CUTOFF
    with pytest.raises(custody.AuthorityError):
        ledger.reserve(*BINDING, "archive", OBJECT)
    now[0] -= 3600
    assert ledger.status(*BINDING).closed
    assert ledger.status(BINDING[0], None, custody.CUTOFF).instance_id == BINDING[1]
    with pytest.raises(custody.AuthorityError):
        ledger.status(BINDING[0], "e" * 64, custody.CUTOFF)


@pytest.mark.parametrize("missing", custody.APPROVALS)
def test_no_implicit_retention_or_authority_to_destroy(tmp_path, missing):
    folder = tmp_path.resolve() / "custody"
    folder.mkdir(mode=0o700)
    policy = approved_test_policy()
    policy[missing] = None
    if missing == "retention_approval":
        policy["retention_days"] = None
    ledger = custody.CustodyLedger.create(folder / "ledger", *BINDING, policy, clock=lambda: custody.CUTOFF)
    result = ledger.status(*BINDING)
    assert result.closed and not result.ready
    if missing != "retention_approval":
        assert not result.purge_authorized
    else:
        assert result.retention_days is None and not result.backup_ready
    completed = ledger.approve(*BINDING, approved_test_policy())
    assert completed.ready and completed.purge_authorized
    with pytest.raises(custody.AuthorityError):
        ledger.approve(*BINDING, approved_test_policy(8))


def test_approved_daily_retention_and_preopening_drill(tmp_path):
    now = [custody.CUTOFF - 5 * 86400]
    folder = tmp_path.resolve() / "custody"
    folder.mkdir(mode=0o700)
    policy = approved_test_policy(2)
    policy["restore_drill"] = None
    ledger = custody.CustodyLedger.create(folder / "ledger", *BINDING, policy, clock=lambda: now[0])
    assert not ledger.status(*BINDING).ready
    assert ledger.reserve(*BINDING, "archive", OBJECT).backup_ready
    assert not ledger.check_archive(*BINDING, OBJECT).closed
    now[0] += 2 * 86400
    with pytest.raises(custody.AuthorityError):
        ledger.check_archive(*BINDING, OBJECT)
    now[0] -= 1
    with pytest.raises(custody.AuthorityError):
        ledger.check_archive(*BINDING, OBJECT)


def test_missing_object_or_store_and_reappearing_copy_prevent_completion(record):
    ledger, now = record
    ledger.reserve(*BINDING, "archive", OBJECT)
    ledger.reserve(*BINDING, "archive", OBJECT)
    assert len(ledger.objects(*BINDING)) == 1
    now[0] = custody.CUTOFF
    ledger.record_purge(*BINDING)
    for kind in set(custody.INVENTORY_KINDS) - {"archive", "snapshot"}:
        ledger.reconcile(*BINDING, kind, RECEIPT)
    with pytest.raises(custody.AuthorityError):
        ledger.reconcile(*BINDING, "archive", RECEIPT)
    ledger.attest_deleted(*BINDING, OBJECT, RECEIPT)
    assert not ledger.reconcile(*BINDING, "archive", RECEIPT).backups_expired
    assert ledger.reconcile(*BINDING, "snapshot", RECEIPT).backups_expired
    unexpected = "9" * 64
    assert not ledger.flag_unknown(*BINDING, unexpected).backups_expired
    assert not ledger.reconcile(*BINDING, "snapshot", RECEIPT).backups_expired
    ledger.discover(*BINDING, "snapshot", unexpected)
    ledger.attest_deleted(*BINDING, unexpected, RECEIPT)
    assert ledger.reconcile(*BINDING, "snapshot", RECEIPT).backups_expired
    assert not ledger.discover(*BINDING, "archive", OBJECT).backups_expired
    with pytest.raises(custody.AuthorityError):
        ledger.check_archive(*BINDING, OBJECT)
    ledger.attest_deleted(*BINDING, OBJECT, "f" * 64)
    assert ledger.reconcile(*BINDING, "archive", "d" * 64).backups_expired
    assert not ledger.reserve(*BINDING, "quarantine", "e" * 64).backups_expired


def test_mutual_tls_separates_readers_and_live_receipts(tmp_path):
    now = [custody.CUTOFF - 1]
    with tls_authority(tmp_path, *BINDING, clock=lambda: now[0], allow_receipt=False) as path:
        reader = custody.Authority(path)
        with pytest.raises(custody.AuthorityError):
            reader.close(*BINDING)
        now[0] += 1
        assert reader.status(*BINDING).closed
        now[0] -= 1
        assert reader.close(*BINDING).closed
        with pytest.raises(custody.AuthorityError):
            reader.record_purge(*BINDING)
        assert not reader.status(*BINDING).purged
        credentials = json.loads(path.read_bytes())
        anon = ssl.create_default_context(cafile=credentials["ca"])
        conn = http.client.HTTPSConnection(reader.host, reader.port, context=anon, timeout=3)
        try:
            with pytest.raises((OSError, http.client.HTTPException)):
                conn.request("POST", "/v1/lifecycle", "{}")
                conn.getresponse()
        finally:
            conn.close()
    with pytest.raises(custody.AuthorityError):
        reader.status(*BINDING)


def test_replayed_or_malformed_proof_rejected(tmp_path, monkeypatch):
    with tls_authority(tmp_path, *BINDING, clock=lambda: custody.CUTOFF - 1) as path:
        live = custody.Authority(path)
        status = asdict(live.status(*BINDING))

        class Recorded:
            def __init__(self, *_args, **_kwargs):
                self.status = 200

            def request(self, *_):
                pass

            def getresponse(self):
                return self

            def read(self, _):
                return json.dumps(
                    dict(nonce="old", event_id=BINDING[0], cutoff=BINDING[2], status=status)
                ).encode()

            def close(self):
                pass

        monkeypatch.setattr(custody.http.client, "HTTPSConnection", Recorded)
        with pytest.raises(custody.AuthorityError):
            live.status(*BINDING)


def test_mtls_latches_with_pinned_live_identity(tmp_path):
    now = [custody.CUTOFF - 1]
    with tls_authority(tmp_path, *BINDING, clock=lambda: now[0]) as path:
        live = custody.Authority(path)
        assert not live.close(*BINDING).purge_authorized
        now[0] += 1
        assert live.record_purge(*BINDING).purged


def test_authority_ownership_and_missing_inputs_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("MAISON_PROD_AUTHORITY", raising=False)
    with pytest.raises(custody.AuthorityError):
        custody.Authority.from_env()
    d = tmp_path.resolve() / "unsafe"
    d.mkdir(mode=0o700)
    d.chmod(0o755)  # The service umask also applies to the test process.
    with pytest.raises(custody.AuthorityError):
        custody.CustodyLedger.create(d / "ledger", *BINDING, approved_test_policy())
    d.chmod(0o700)
    symlink = tmp_path.resolve() / "link"
    symlink.symlink_to(d)
    with pytest.raises(custody.AuthorityError):
        custody.CustodyLedger.create(symlink / "ledger", *BINDING, approved_test_policy())
