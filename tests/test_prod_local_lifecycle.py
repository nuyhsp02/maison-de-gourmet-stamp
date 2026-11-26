"""Single-host gates. Every DB, policy, clock and contact is a temporary fixture."""
from contextlib import closing
import hashlib
import json
import os
import secrets
import socket
import sqlite3

import pytest

from lifecycle_fixtures import fixture_epoch, provision_local_controls
from prod_fixtures import ORIGIN, prod, ready_state
import prod_local_lifecycle as local
from test_prod_enrollment import HEADERS, NAME, NUMBER, REGISTER, approval, read_policy

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux PROD filesystem contract")


@pytest.fixture
def live(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    state, config, descriptor, recipient = ready_state(root)
    bound, ledger, decision = provision_local_controls(root, state, config, monkeypatch)
    now = [fixture_epoch()]
    def clock():
        if now[0] is None:
            raise local.AuthorityError("Fixture clock unsynchronized")
        return now[0]
    monkeypatch.setattr(local, "synchronized_time", clock)
    monkeypatch.setattr(prod.time, "time", lambda: now[0] or fixture_epoch())
    return state, config, descriptor, recipient, now, bound, ledger, decision


def test_default_runtime_without_network_can_register_find_collect_redeem_and_use_staff(live, monkeypatch):
    state, config, descriptor, recipient, _, _, _, _ = live
    blocked_calls = []
    def unreachable(*args, **kwargs):
        blocked_calls.append(1)
        raise OSError("Synthetic storage or Mac is offline")
    monkeypatch.setattr(socket.socket, "connect", unreachable)
    # Prove the firewall simulation is live, then verify customer traffic makes zero attempts.
    with socket.socket() as probe, pytest.raises(OSError):
        probe.connect(("127.0.0.1", 9))
    blocked_calls.clear()
    recipient = read_policy(descriptor, config.parent / "notice.json", approval(recipient.key_id))
    prod.check(state, config, ORIGIN, recipient)
    app = prod.prod_app(state, config, ORIGIN, recipient)
    signup, recovery, staff, manager = (app.test_client() for _ in range(4))
    def post(client, url, payload, csrf=None):
        headers = dict(HEADERS)
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        return client.post(url, json=payload, headers=headers, base_url=ORIGIN)
    assert post(signup, "/api/register", REGISTER).status_code == 200
    assert post(recovery, "/api/find", {"name": NAME, "phone": NUMBER}).status_code == 200
    csrf = recovery.get("/api/state", base_url=ORIGIN).json["csrf"]
    database = prod.database_path(state)
    with closing(prod.application.connect_db(database, mode="ro")) as db:
        keys = [r[0] for r in db.execute("SELECT qr_key FROM products")]
    for qr in keys:
        assert post(recovery, "/api/collect", {"qr": qr}, csrf).status_code == 200
    assert len(keys) == 10
    assert post(recovery, "/api/redeem", {"confirm_received": True}, csrf).json["already_redeemed"] is False
    assert post(recovery, "/api/redeem", {"confirm_received": True}, csrf).json["already_redeemed"] is True
    for client, role, login in ((staff, "staff", "login"), (manager, "admin", "manager-login")):
        password = secrets.token_urlsafe(30)
        prod.staff_admin.write_staff_password(database, "fixture-" + role, password, role=role, production=True)
        auth = post(client, "/api/admin/" + login, {"password": password}).json
        assert auth["role"] == role
        assert client.get("/api/admin/stats", base_url=ORIGIN).status_code == 200
        found = client.post("/api/admin/search", json={"phone": NUMBER}, base_url=ORIGIN,
                            headers={**HEADERS, "X-Admin-CSRF": auth["csrf"]})
        assert found.status_code == 200 and found.json["phone_masked"] == prod.contact.masked(NUMBER)
        assert NUMBER not in found.text
    assert not blocked_calls


def test_untrusted_clock_and_clock_rollback_fail_closed_then_recover(live):
    state, config, _, recipient, now, _, _, _ = live
    c = prod.prod_app(state, config, ORIGIN, recipient).test_client()
    assert c.get("/api/state", base_url=ORIGIN).status_code == 200
    now[0] = None
    assert c.get("/api/state", base_url=ORIGIN).status_code == 503
    now[0] = fixture_epoch() - 120
    with pytest.raises(local.AuthorityError):
        prod.prod_app(state, config, ORIGIN, recipient)
    assert c.get("/api/state", base_url=ORIGIN).status_code == 503
    now[0] = fixture_epoch() + 1
    assert c.get("/api/state", base_url=ORIGIN).status_code == 200


@pytest.mark.parametrize("fault", ["missing_approval", "wrong_pin", "changed_binding", "changed_policy",
                                    "overlong_retention", "missing_state", "untrusted_owner"])
def test_required_local_trust_inputs(live, monkeypatch, fault):
    state, config, _, recipient, _, _, ledger, decision = live
    app = prod.prod_app(state, config, ORIGIN, recipient)
    if fault == "missing_approval":
        decision.unlink()
    elif fault == "wrong_pin":
        monkeypatch.setenv("MAISON_PROD_LIFECYCLE_APPROVAL_SHA256", "0"*64)
    elif fault == "missing_state":
        ledger.unlink()
    elif fault == "untrusted_owner":
        monkeypatch.setattr(local, "APPROVAL_OWNER_UID", os.geteuid() + 1)
    elif fault == "changed_policy":
        with closing(sqlite3.connect(ledger)) as db, db:
            row = json.loads(db.execute("SELECT policy FROM event").fetchone()[0])
            row["audit_deletion_approval"] = "f" * 64
            db.execute("UPDATE event SET policy=?", (json.dumps(row),))
    else:
        obj = json.loads(decision.read_bytes())
        if fault == "changed_binding":
            obj["instance_id"] = "e" * 64
        else:
            obj["policy"]["retention_days"] = 365
        decision.write_text(json.dumps(obj))
        monkeypatch.setenv("MAISON_PROD_LIFECYCLE_APPROVAL_SHA256", hashlib.sha256(decision.read_bytes()).hexdigest())
    assert app.test_client().get("/api/state", base_url=ORIGIN).status_code == 503
    with pytest.raises((OSError, ValueError)):
        prod.prod_app(state, config, ORIGIN, recipient)
    if fault == "missing_state":
        assert not ledger.exists()


def test_powered_off_over_deadline_reboot_repurges_an_old_db_and_config(live, tmp_path):
    state, config, _, recipient, now, bound, _, _ = live
    dbfile = prod.database_path(state)
    initial = prod.prod_app(state, config, ORIGIN, recipient).test_client()
    assert initial.get("/api/state", base_url=ORIGIN).status_code == 200
    with closing(sqlite3.connect(dbfile)) as db, db:
        db.execute("INSERT INTO customers VALUES ('synthetic-preclose',1,NULL)")
        db.commit()
        # Snapshot an old fully valid DB, including its instance id, using SQLite's backup API.
        with closing(sqlite3.connect(tmp_path / "old.sqlite")) as past:
            db.backup(past)
    old_config = config.read_bytes()
    now[0] = bound[2]
    def reboot_and_verify():
        cli = prod.prod_app(state, config, ORIGIN, recipient).test_client()
        assert cli.get("/api/state", base_url=ORIGIN).status_code == 410
        assert "행사가 종료되었습니다" in cli.get("/", base_url=ORIGIN).text
        with closing(sqlite3.connect(dbfile)) as db:
            assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 0
        report = local.evidence(local.local_ledger(), local.read_approval())
        assert report["closure"] and report["local_db_purge_verified_at"] == bound[2]
        assert not report["backups_expired"]
        assert "synthetic-preclose" not in json.dumps(report)
    reboot_and_verify()
    with closing(sqlite3.connect(tmp_path / "old.sqlite")) as past, closing(sqlite3.connect(dbfile)) as live_db:
        past.backup(live_db)
        live_db.execute("PRAGMA journal_mode=WAL")
    config.write_bytes(old_config)
    now[0] = bound[2] - 1
    reboot_and_verify()


def test_kernel_ntp_error_blocks_use_of_the_wall_clock(monkeypatch):
    monkeypatch.setattr(local, "_query_clock", lambda: lambda _buf: 5)
    with pytest.raises(local.AuthorityError):
        local.synchronized_time()


def test_explicit_initialization_approval_and_evidence_cli(live, monkeypatch, tmp_path, capsys):
    state, config, _, recipient, _, bound, _, decision = live
    folder = tmp_path.resolve() / "separate-control"
    folder.mkdir(mode=0o700)
    path = folder / "journal.sqlite"
    def publish(doc):
        decision.write_text(json.dumps(doc))
        monkeypatch.setenv("MAISON_PROD_LIFECYCLE_APPROVAL_SHA256", hashlib.sha256(decision.read_bytes()).hexdigest())
    def cli(action):
        monkeypatch.setattr(local.sys, "argv", ["prod_local_lifecycle.py", action, "--ledger", str(path)])
        local.main()
        return json.loads(capsys.readouterr().out)
    doc = json.loads(decision.read_bytes())
    doc["policy"]["restore_drill"] = None
    publish(doc)
    monkeypatch.setenv("MAISON_PROD_LIFECYCLE", str(path))
    # Unlike the CLI in production, this test process also runs other test cases.
    prior = os.umask(0o077)
    try:
        initial = cli("init")
        assert initial["event_id"] == bound[0] and not initial["closure"]
        with pytest.raises(SystemExit):
            cli("init")
        with pytest.raises(prod.ProdError):
            prod.prod_app(state, config, ORIGIN, recipient)
        doc["policy"]["restore_drill"] = "d" * 64
        publish(doc)
        with pytest.raises(local.AuthorityError):
            prod.prod_app(state, config, ORIGIN, recipient)
        cli("approve")
        assert prod.prod_app(state, config, ORIGIN, recipient).test_client().get("/api/state", base_url=ORIGIN).status_code == 200
        assert cli("evidence")["approval_sha256"] == hashlib.sha256(decision.read_bytes()).hexdigest()
        doc["policy"]["audit_deletion_approval"] = "b" * 64
        publish(doc)
        with pytest.raises(SystemExit):
            cli("approve")
    finally:
        os.umask(prior)


def test_isolated_candidate_cannot_start_or_release_contacts_via_live_entry(live):
    state, config, _, recipient, _, _, _, _ = live
    # Move an exact instance including both valid roles to simulate an authorized repair
    # candidate. Only explicit promotion into the operator's active root may serve it.
    other = state.with_name("isolated-recovery-candidate")
    state.rename(other)
    with pytest.raises(prod.ProdError, match="active service root"):
        prod.prod_app(other, config, ORIGIN, recipient)
    with pytest.raises(prod.ProdError, match="active service root"):
        prod.PrivacyAccess(other, prod.load_prod_event(config), recipient)
