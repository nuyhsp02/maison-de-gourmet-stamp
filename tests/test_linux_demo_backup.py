import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux DEMO permissions and OpenSSL 3")


@pytest.fixture
def backup(tmp_path):
    sys.path.insert(0, str(ROOT / "deploy/linux"))
    try:
        import backup as tool
        import demo
    finally:
        sys.path.remove(str(ROOT / "deploy/linux"))
    event = json.loads((ROOT / "config/event.example.json").read_text())
    now = datetime.now(timezone.utc)
    event.update(id="demo-test", draft=False, starts_at=(now - timedelta(days=1)).isoformat(),
                 ends_at=(now + timedelta(days=1)).isoformat(), reward_until=(now + timedelta(days=2)).isoformat())
    config = tmp_path / "event.json"
    config.write_text(json.dumps(event))
    state = demo.prepare_migration(tmp_path.resolve() / "drill", config, "https://demo.example.invalid",
                                   purpose="contacts+corrections").target
    return tool, state, config


@pytest.fixture(scope="module")
def recipient(tmp_path_factory):
    vault = tmp_path_factory.mktemp("demo-vault")
    cert, key = vault / "recipient.pem", vault / "identity.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", key, "-out", cert, "-subj", "/CN=local-demo-test"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    return cert, key


def test_encrypted_online_snapshot_roundtrip_audits_qr_and_key(backup, recipient, tmp_path):
    tool, state, config = backup
    cert, identity = recipient
    db = state / tool.DATABASE_NAME
    archive = tmp_path / "demo.cms"
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO customers VALUES ('fixture-a', 10, 40)")
        writer.execute("INSERT INTO customer_profiles VALUES ('fixture-a', 'Synthetic', 'fixture-hmac', 'v1', 11)")
        writer.execute("INSERT INTO stamps VALUES ('fixture-a','season-01',20)")
        writer.execute("INSERT INTO reward_audit VALUES ('fixture-a',40,'unauthenticated_customer_screen')")
        writer.execute("INSERT INTO stamp_cancel_audit VALUES (1,'fixture-a','season-02',23,30,'fixture-staff')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO customers VALUES ('uncommitted', 100, NULL)")
        counts = tool.create(state, config, cert, archive)
        writer.rollback()

    assert counts == dict(products=10, customers=1, customer_profiles=1, stamps=1, reward_audit=1, stamp_cancel_audit=1)
    encoded = archive.read_bytes()
    assert b"SQLite format 3" not in encoded and b"fixture-staff" not in encoded
    assert (state / tool.PROFILE_KEY).read_bytes() not in encoded
    assert archive.stat().st_mode & 0o777 == 0o600
    target = tmp_path / "recovery"
    assert tool.restore(archive, config, cert, identity, target) == counts
    assert target.stat().st_mode & 0o777 == 0o700
    assert {p.name for p in target.iterdir()} == {tool.DATABASE_NAME, tool.PROFILE_KEY}
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    assert (target / tool.PROFILE_KEY).read_bytes() == (state / tool.PROFILE_KEY).read_bytes()
    with closing(tool.ops.readonly(db)) as live, closing(tool.ops.readonly(target / tool.DATABASE_NAME)) as staged:
        assert staged.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        for table in ("products", "customers", "customer_profiles", "stamps", "reward_audit", "stamp_cancel_audit"):
            query = "SELECT * FROM " + table + " ORDER BY 1"
            assert live.execute(query).fetchall() == staged.execute(query).fetchall()

    with pytest.raises(ValueError):
        tool.create(state, config, cert, archive)
    assert archive.read_bytes() == encoded
    with pytest.raises(FileExistsError):
        tool.restore(archive, config, cert, identity, target)
    damaged = tmp_path / "damaged.cms"
    damaged.write_bytes(encoded[:-20])
    failed = tmp_path / "failed"
    with pytest.raises(subprocess.CalledProcessError):
        tool.restore(damaged, config, cert, identity, failed)
    assert not failed.exists()


@pytest.mark.parametrize("fault", ["missing-db", "wrong-key", "missing-key", "foreign-key", "unmarked"])
def test_create_refuses_invalid_pair_and_leaves_no_archive(backup, recipient, tmp_path, fault):
    tool, state, config = backup
    cert, _ = recipient
    db, key = state / tool.DATABASE_NAME, state / tool.PROFILE_KEY
    if fault == "missing-db":
        db.unlink()
    elif fault == "wrong-key":
        key.write_bytes(os.urandom(32))
    elif fault == "missing-key":
        key.unlink()
    else:
        with closing(sqlite3.connect(db)) as writer:
            if fault == "foreign-key":
                writer.execute("PRAGMA foreign_keys=OFF")
                writer.execute("INSERT INTO stamps VALUES ('missing-customer','season-01',1)")
            else:
                writer.execute("DELETE FROM metadata WHERE key='linux_demo_v1'")
            writer.commit()
    output = tmp_path / "candidate.cms"
    with pytest.raises((ValueError, OSError, RuntimeError, tool.ops.OpsError)):
        tool.create(state, config, cert, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".demo-backup-*"))
    if fault == "missing-db":
        assert not db.exists()
    if fault == "missing-key":
        assert not key.exists()


@pytest.mark.parametrize("fault", ["wrong-key", "extra-member"])
def test_restore_requires_exact_members_and_valid_pair(backup, recipient, tmp_path, fault):
    tool, state, config = backup
    cert, identity = recipient
    package = tmp_path / "fixture.zip"
    with zipfile.ZipFile(package, "x") as bundle:
        bundle.write(tool.ops.snapshot(state / tool.DATABASE_NAME, tmp_path / "detached.sqlite"), tool.DATABASE_NAME)
        if fault == "wrong-key":
            bundle.writestr(tool.PROFILE_KEY, os.urandom(32))
        else:
            bundle.write(state / tool.PROFILE_KEY, tool.PROFILE_KEY)
            bundle.writestr("../outside", b"should not be extracted")
    archive = tmp_path / "fixture.cms"
    subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-outform", "DER", "-aes-256-gcm",
                    "-recip", cert, "-in", package, "-out", archive], check=True)
    target = tmp_path / "new-target"
    with pytest.raises(ValueError):
        tool.restore(archive, config, cert, identity, target)
    assert not target.exists()
    assert not (tmp_path / "outside").exists()


def test_restored_service_recovers_customer_with_same_phone_and_stamp(backup, recipient, tmp_path):
    from secrets import token_urlsafe
    from demo import demo_app
    from staff_admin import write_staff_password

    tool, state, config = backup
    cert, identity = recipient
    origin = "https://demo.example.invalid"
    headers = {"Origin": origin, "X-Stamp-Client": "1"}
    fields = {"name": "가상 고객", "phone": "010-0000-0000", "notice_acknowledged": True, "notice_version": "synthetic-contact-v1"}
    write_staff_password(state / tool.DATABASE_NAME, "demo-staff", token_urlsafe())
    from demo import contact, load_demo_event, migrate_contacts
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from contact_fixture import write_config
    policy = write_config(contact, tmp_path / "synthetic.json", load_demo_event(config)["id"], [(fields["name"], "01000000000")])
    migrate_contacts(state, config, origin, policy, source_state_dir=state.parent / "source",
                     expected_event_id="demo-test", expected_schema="base")
    visitor = demo_app(state, config, origin, policy).test_client()
    assert visitor.post("/api/register", json=fields, headers=headers, base_url=origin).status_code == 200
    csrf = visitor.get("/api/state", base_url=origin).json["csrf"]
    with closing(tool.ops.readonly(state / tool.DATABASE_NAME)) as live:
        qr = live.execute("SELECT qr_key FROM products WHERE id='season-01'").fetchone()[0]
    assert visitor.post("/api/collect", json={"qr": qr}, headers={**headers, "X-CSRF-Token": csrf},
                        base_url=origin).status_code == 200

    archive, target = tmp_path / "drill.cms", tmp_path / "restored"
    tool.create(state, config, cert, archive)
    tool.restore(archive, config, cert, identity, target)
    with pytest.raises(ValueError):
        demo_app(target, config, origin)
    write_staff_password(target / tool.DATABASE_NAME, "demo-staff", token_urlsafe())
    recovered = demo_app(target, config, origin, policy).test_client()
    assert recovered.post("/api/find", json={"name": fields["name"], "phone": "+82 10 0000 0000"},
                          headers=headers, base_url=origin).status_code == 200
    customer = recovered.get("/api/state", base_url=origin).json["customer"]
    assert customer["count"] == 1 and "season-01" in customer["stamps"]
    assert tool.summary(target / tool.DATABASE_NAME)["customers"] == 1


def test_correction_audit_is_append_only_and_survives_encrypted_restore(backup, recipient, tmp_path):
    import demo

    tool, state, config = backup
    cert, identity = recipient
    path = state / demo.DATABASE_NAME
    demo.migrate_corrections(state, config, "https://demo.example.invalid", source_state_dir=state.parent / "source",
                             expected_event_id="demo-test", expected_schema="base")
    entry = (1, "f" * 32, "fixture-card", "fixture-manager", 3, "replace", "wrong_product",
             "synthetic product mismatch", "season-01", "season-02", 2, 1, 0, 0)
    with closing(demo.connect_db(path, mode="rw")) as writer, writer:
        writer.execute("INSERT INTO customers VALUES ('fixture-card', 1, NULL)")
        writer.execute("INSERT INTO stamps VALUES ('fixture-card', 'season-02', 2)")
        writer.execute("INSERT INTO stamp_correction_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", entry)
        for sql in ("UPDATE stamp_correction_audit SET reason_detail='changed reason text'",
                    "DELETE FROM stamp_correction_audit",
                    "INSERT OR REPLACE INTO stamp_correction_audit SELECT * FROM stamp_correction_audit"):
            with pytest.raises(sqlite3.IntegrityError):
                writer.execute(sql)
    archive, destination = tmp_path / "corrections.cms", tmp_path / "staging"
    counts = tool.create(state, config, cert, archive)
    assert counts["stamp_correction_audit"] == 1
    encoded = (json.dumps(entry, separators=(",", ":")) + "\n").encode("ascii")
    assert counts["correction_audit_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert tool.restore(archive, config, cert, identity, destination) == counts
    staged = destination / demo.DATABASE_NAME
    demo.validate_database(staged, demo.load_demo_event(config), require_wal=True)
    with closing(demo.connect_db(staged, mode="ro")) as restored:
        assert tuple(restored.execute("SELECT * FROM stamp_correction_audit").fetchone()) == entry
        assert tuple(restored.execute("SELECT * FROM stamps").fetchone()) == ("fixture-card", "season-02", 2)


def test_restore_disarms_previously_active_manager_even_with_original_verifier(backup, recipient, tmp_path):
    import demo
    from secrets import token_urlsafe
    from staff_admin import manager_key_path, write_staff_password

    tool, state, config = backup
    db = state / tool.DATABASE_NAME
    origin = "https://demo.example.invalid"
    headers = {"Origin": origin, "X-Stamp-Client": "1"}
    old, new, regular = token_urlsafe(24), token_urlsafe(24), token_urlsafe(24)
    write_staff_password(db, "counter", regular)
    write_staff_password(db, "manager", old, role="admin")
    manager = demo.demo_app(state, config, origin).test_client()
    login = "/api/admin/manager-login"
    assert manager.post(login, json={"password": old}, headers=headers, base_url=origin).status_code == 200
    cookie = manager.get_cookie("__Host-stamp-admin", domain="demo.example.invalid")
    archive, target = tmp_path / "manager.cms", tmp_path / "staged"
    cert, identity = recipient
    counts = tool.create(state, config, cert, archive)
    assert tool.restore(archive, config, cert, identity, target) == counts
    # Even separately restoring the exact verifier cannot reactivate archived privileges.
    staged_db = target / tool.DATABASE_NAME
    path = manager_key_path(staged_db)
    assert not path.exists()
    path.write_bytes(manager_key_path(db).read_bytes())
    path.chmod(0o600)
    write_staff_password(staged_db, "counter", regular)
    client = demo.demo_app(target, config, origin).test_client()
    client.set_cookie(cookie.key, cookie.value, domain=cookie.domain)
    assert client.get("/api/admin/session", base_url=origin).status_code == 401
    assert client.post(login, json={"password": old}, headers=headers, base_url=origin).status_code == 503
    assert manager.get("/api/admin/session", base_url=origin).status_code == 200
    write_staff_password(staged_db, "manager", new, role="admin")
    assert client.post(login, json={"password": old}, headers=headers, base_url=origin).status_code == 401
    assert client.post(login, json={"password": new}, headers=headers, base_url=origin).status_code == 200
