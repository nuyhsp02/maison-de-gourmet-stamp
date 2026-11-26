"""No cloud calls: exercise encrypted production snapshots against a versioned test store."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux file ownership and OpenSSL CMS")


class Store:
    data = "synthetic-data"
    evidence = "synthetic-evidence"

    def __init__(self):
        self.objects = {}
        self.offline = False
        self.fail_receipt = False
        self.hold = False
        self.snapshots = False
        self.beats = []

    def ready(self):
        if self.offline:
            raise ValueError("fixture network failure")

    def uploads_clear(self, prefix):
        pass

    def snapshots_clear(self):
        if self.snapshots:
            raise ValueError("fixture provider has a snapshot")

    def heartbeat(self, event):
        self.beats.append(event)

    def put(self, bucket, key, file, digest):
        data = file.read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        if bucket == self.evidence and self.fail_receipt:
            raise ValueError("fixture response failure")
        version = digest[:12]
        prior = self.objects.setdefault((bucket, key, version), data)
        assert prior == data
        return version

    def versions(self, bucket, prefix):
        return {
            (key, v): ("Versions" if data is not None else "DeleteMarkers")
            for (b, key, v), data in self.objects.items()
            if b == bucket and key.startswith(prefix)
        }

    def delete(self, key, version):
        if not self.hold:
            del self.objects[self.data, key, version]


@pytest.fixture(scope="module")
def public_cert(tmp_path_factory):
    directory = tmp_path_factory.mktemp("object-custodian").resolve()
    cert, identity = directory / "fixture.pem", directory / "fixture.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            identity,
            "-out",
            cert,
            "-subj",
            "/CN=synthetic",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    der = subprocess.check_output(["openssl", "x509", "-in", cert, "-outform", "DER"])
    return cert, hashlib.sha256(der).hexdigest()


@pytest.fixture
def setup(tmp_path, monkeypatch, public_cert):
    from prod_fixtures import ready_state
    import prod
    import prod_backup
    import prod_backup_custody as custody
    import prod_local_lifecycle as lifecycle
    from prod_custody import APPROVALS, CUTOFF, CustodyLedger

    root = tmp_path.resolve()
    state, event, descriptor, recipient = ready_state(root)
    with closing(sqlite3.connect(prod.database_path(state))) as db:
        instance_id = dict(db.execute("SELECT key,value FROM metadata"))["prod_instance_id"]
    cfg = dict(
        state_dir=state,
        event=event,
        recipient=descriptor,
        certificate=public_cert[0],
        cert_sha256=public_cert[1],
        instance_id=instance_id,
        spool=root / "spool",
        work=root / "work",
        posix_receipts=root / "proofs",
        region="fixture",
        aws_account="synthetic-owner",
    )
    for name in ("spool", "work", "posix_receipts"):
        cfg[name].mkdir(mode=0o700)
    cdir = root / "control"
    cdir.mkdir(mode=0o700)
    clock = [CUTOFF - 25 * 86400]
    ledger = CustodyLedger.create(
        cdir / "policy.sqlite",
        "prod-synthetic-fixture",
        instance_id,
        CUTOFF,
        {**dict.fromkeys(APPROVALS, "a" * 64), "retention_days": 14},
        clock=lambda: clock[0],
    )
    volumes = [
        dict(path=str(state), volume_id="a" * 32, kinds=["temporary"], service=True),
        dict(path=str(cfg["spool"]), volume_id="b" * 32, kinds=["archive", "temporary"], service=False),
    ]
    marker = cfg["spool"] / custody.MARKER
    marker.write_text("b" * 32)
    marker.chmod(0o600)
    plan = cdir / "roots.json"
    plan.write_text(
        json.dumps(
            dict(
                format="prod-backup-roots-v1",
                event_id="prod-synthetic-fixture",
                instance_id=instance_id,
                volumes=volumes,
                manual_kinds=["external", "snapshot"],
            )
        )
    )
    approval = cdir / "lifecycle.json"
    approval.write_text(
        json.dumps(
            dict(
                format="prod-lifecycle-approval-v1",
                event_id="prod-synthetic-fixture",
                instance_id=instance_id,
                access_until=CUTOFF,
                policy={**dict.fromkeys(APPROVALS, "a" * 64), "retention_days": 14},
            )
        )
    )
    monkeypatch.setattr(lifecycle, "APPROVAL_OWNER_UID", os.getuid(), raising=False)
    monkeypatch.setenv("MAISON_PROD_LIFECYCLE_APPROVAL", str(approval))
    monkeypatch.setenv(
        "MAISON_PROD_LIFECYCLE_APPROVAL_SHA256", hashlib.sha256(approval.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("MAISON_PROD_BACKUP_ROOTS", str(plan))
    monkeypatch.setenv("MAISON_PROD_BACKUP_ROOTS_SHA256", hashlib.sha256(plan.read_bytes()).hexdigest())
    monkeypatch.setattr(prod_backup, "time", SimpleNamespace(time=lambda: clock[0]))
    return cfg, ledger, Store(), clock


def test_network_failure_retains_ciphertext_and_service_stays_available(setup):
    import prod_object_backup as jobs
    import prod
    from prod_fixtures import ORIGIN

    cfg, ledger, store, _ = setup
    store.offline = True
    with pytest.raises(ValueError, match="fixture network"):
        jobs.run(cfg, ledger, store)
    files = list(cfg["spool"].glob("*.cms"))
    assert len(files) == 1 and b"SQLite format 3" not in files[0].read_bytes()
    event = prod.load_prod_event(cfg["event"])
    rec = prod.contact.ProductionEnrollment.read(cfg["recipient"], event["id"])
    client = prod.prod_app(cfg["state_dir"], cfg["event"], ORIGIN, rec, authority=ledger).test_client()
    assert client.get("/api/state", base_url=ORIGIN).status_code == 200
    store.offline = False
    assert jobs.run(cfg, ledger, store)["storage_verified"]
    backup = next(key for key in store.objects if key[0] == store.data)
    assert store.objects[backup] == files[0].read_bytes()
    receipt = json.loads(
        next(
            data
            for (b, k, _), data in store.objects.items()
            if b == store.evidence and k.startswith("received/")
        )
    )
    assert receipt["archive_sha256"] == hashlib.sha256(files[0].read_bytes()).hexdigest()
    assert receipt["object_key"] == backup[1] and receipt["version_id"] == backup[2]


def test_retry_after_cloud_accepted_body_but_receipt_failed(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, _ = setup
    store.fail_receipt = True
    with pytest.raises(ValueError, match="fixture response"):
        jobs.run(cfg, ledger, store)
    ciphertext = dict(store.objects)
    assert len(ciphertext) == 1 and not store.beats
    store.fail_receipt = False
    jobs.run(cfg, ledger, store)
    jobs.run(cfg, ledger, store)
    assert all(store.objects[k] == v for k, v in ciphertext.items())
    assert len(list(cfg["spool"].glob("*.cms"))) == 1
    assert {key.split("/")[0] for b, key, v in store.objects if b == store.evidence} == {
        "received",
        "policy",
        "lifecycle",
    }


def test_exact_fourteen_days_removes_noncurrent_versions_and_markers(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    jobs.run(cfg, ledger, store)
    b, key, v = next(k for k in store.objects if k[0] == store.data)
    item = (b, key, v)
    first = clock[0]
    clock[0] = first + jobs.RETENTION - 1
    jobs.run(cfg, ledger, store)
    assert item in store.objects
    store.objects[b, key, "prior"] = store.objects[item]
    store.objects[b, key, "marker"] = None
    clock[0] += 1
    jobs.run(cfg, ledger, store)
    assert not store.versions(store.data, key)
    assert not (cfg["spool"] / Path(key).name).exists()
    receipt = json.loads(
        next(data for (b, k, _), data in store.objects.items() if k == "expired/" + key + ".json")
    )
    assert receipt["verified_remaining"] == 0 and set(receipt["versions"]) == {v, "prior", "marker"}


def test_cutoff_and_refused_deletion_do_not_report_false_success(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    clock[0] = jobs.CUTOFF - 10
    jobs.run(cfg, ledger, store)
    clock[0] = jobs.CUTOFF
    store.hold = True
    with pytest.raises(ValueError, match="still present"):
        jobs.run(cfg, ledger, store)
    assert not list(cfg["spool"].glob("*.cms"))
    assert not any(k.startswith(("expired/", "reconciled/")) for _, k, _ in store.objects)
    store.hold = False
    assert jobs.run(cfg, ledger, store)["closed"]
    assert not store.versions(store.data, "prod-backups/")
    assert any(k.startswith("reconciled/") for _, k, _ in store.objects)
    assert not ledger.status("prod-synthetic-fixture", cfg["instance_id"], jobs.CUTOFF).backups_expired


def test_health_detects_missing_receipt_and_offline_cutoff_still_expires_spool(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    jobs.run(cfg, ledger, store)
    bk = next(k for k in store.objects if k[0] == store.data)
    del store.objects[bk]
    with pytest.raises(ValueError, match="verified off-host"):
        jobs.run(cfg, ledger, store, health_only=True)
    clock[0] = jobs.CUTOFF
    store.offline = True
    with pytest.raises(ValueError, match="fixture network"):
        jobs.run(cfg, ledger, store)
    assert len(list(cfg["spool"].glob("*.cms"))) == 0


def test_unknown_object_or_provider_snapshot_prevents_green_receipt(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    jobs.run(cfg, ledger, store)
    bk = next(k for k in store.objects if k[0] == store.data)
    extra = (store.data, bk[1] + ".unknown", "unknown-version")
    store.objects[extra] = b"synthetic"
    clock[0] = jobs.CUTOFF
    with pytest.raises(ValueError, match="Unknown or reappeared"):
        jobs.run(cfg, ledger, store)
    assert extra in store.objects
    del store.objects[extra]
    store.snapshots = True
    with pytest.raises(ValueError, match="provider has a snapshot"):
        jobs.run(cfg, ledger, store)
    assert not any(k.startswith("reconciled/") for _, k, _ in store.objects)


def test_lost_delivery_journal_fails_even_if_storage_was_emptied(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, _ = setup
    jobs.run(cfg, ledger, store)
    (cfg["work"] / "delivery.sqlite").unlink()
    store.objects.clear()
    with pytest.raises(ValueError, match="Missing or mismatched delivery journal"):
        jobs.run(cfg, ledger, store)
    assert not (cfg["work"] / "delivery.sqlite").exists()


def test_off_host_receipt_loss_is_detected_before_expiry(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, _ = setup
    jobs.run(cfg, ledger, store)
    receipt = next(ref for ref in store.objects if ref[1].startswith("received/"))
    del store.objects[receipt]
    with pytest.raises(ValueError, match="delivery receipt missing"):
        jobs.run(cfg, ledger, store, health_only=True)


def test_outage_exceeding_freshness_window_is_detected(setup):
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    jobs.run(cfg, ledger, store)
    clock[0] += jobs.FRESH
    with pytest.raises(ValueError, match="receipt is missing or stale"):
        jobs.run(cfg, ledger, store, health_only=True)


@contextmanager
def _source_pair():
    import socket

    server, client = socket.socketpair(type=socket.SOCK_SEQPACKET)
    with server, client:
        yield server, client


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux credentialed UNIX socket")
def test_socket_source_transfers_only_ciphertext_and_expires_at_exact_boundary(setup):
    import socket
    from concurrent.futures import ThreadPoolExecutor
    import prod_backup_source as source
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    cfg.update(data_bucket=store.data, evidence_bucket=store.evidence)
    ctx = source.binding(cfg)
    owner = source.SnapshotSource(cfg, ledger, jobs.destination_binding(cfg, ctx))
    with ThreadPoolExecutor(max_workers=1) as pool, _source_pair() as (server, client):
        server.settimeout(30)
        client.settimeout(30)
        worker = pool.submit(source.serve, server, owner, os.getuid())
        transfer = source.RemoteSource(client, clock=lambda: clock[0])
        assert jobs.transfer(cfg, ctx, transfer, store)["storage_verified"]
        local, = cfg["spool"].glob("*.cms")
        original = local.read_bytes()
        assert b"SQLite format 3" not in original
        assert next(v for (bucket, _, _), v in store.objects.items() if bucket == store.data) == original
        assert [p.name for p in cfg["work"].iterdir()] == ["delivery.sqlite"]
        clock[0] += jobs.RETENTION
        assert jobs.transfer(cfg, ctx, transfer, store)["storage_verified"]
        assert not local.exists()
        assert original not in store.objects.values()
        assert any(key.startswith("expired/") for _, key, _ in store.objects)
        client.shutdown(socket.SHUT_RDWR)
        worker.result(timeout=5)


def test_source_refuses_unregistered_expired_and_early_retired_archives(setup):
    import prod_backup_source as source
    import prod_object_backup as jobs

    cfg, ledger, store, clock = setup
    cfg.update(data_bucket=store.data, evidence_bucket=store.evidence)
    jobs.run(cfg, ledger, store)
    ctx = source.binding(cfg)
    local = source.SnapshotSource(cfg, ledger, jobs.destination_binding(cfg, ctx))
    name = next(cfg["spool"].glob("*.cms")).name
    with pytest.raises(ValueError, match="not due"):
        local.retired(name, "f" * 64)
    with pytest.raises(ValueError, match="Unexpected backup archive name"):
        local.archive("../" + name)
    alias = cfg["spool"] / ("2027-03-06-" + "0" * 24 + ".cms")
    alias.write_bytes(b"unregistered")
    alias.chmod(0o600)
    with pytest.raises(ValueError, match="unknown"):
        local.archive(alias.name)
    clock[0] += jobs.RETENTION
    with pytest.raises(ValueError, match="retention"):
        local.archive(name)
    with pytest.raises(ValueError, match="retention"):
        local.reserve(name)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux credentialed UNIX socket")
def test_socket_peer_operation_clock_and_fd_restrictions(tmp_path):
    import socket
    import prod_backup_source as source
    from array import array

    with _source_pair() as (sender, receiver):
        with pytest.raises(PermissionError, match="peer UID"):
            source.check_peer(receiver, os.getuid() + 1)
        sender.sendall(b'["unlisted_method"]')
        with pytest.raises(ValueError, match="Unknown source operation"):
            source.serve(receiver, object(), os.getuid())
        sender.sendall(b'[50,false]')
        client = source.RemoteSource(receiver, clock=lambda: 25)
        with pytest.raises(ValueError, match="clock differ"):
            client.clock()
        with (tmp_path / "synthetic").open("wb") as writable:
            sender.sendmsg([b"null"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [writable.fileno()]))])
            with pytest.raises(ValueError, match="Invalid encrypted archive descriptor"):
                client.archive("ignored-on-fixture")


def test_evidence_with_extra_or_untyped_fields_is_never_uploaded(setup):
    import prod_backup_source as source
    import prod_object_backup as jobs

    cfg, ledger, store, _ = setup
    cfg.update(data_bucket=store.data, evidence_bucket=store.evidence)
    ctx = source.binding(cfg)
    local = source.SnapshotSource(cfg, ledger, jobs.destination_binding(cfg, ctx))
    raw, original = local.evidence()
    for change in ({"extra": "unset"}, {"inventory_outstanding": "one"}, {"closure": 0}):
        bad = SimpleNamespace(evidence=lambda: (raw, dict(original, **change)))
        with pytest.raises(ValueError, match="Evidence does not match"):
            jobs.export_lifecycle(store, bad, cfg["work"], "fixture/", ctx)
        assert not store.objects
