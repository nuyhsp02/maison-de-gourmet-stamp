"""All records, credentials and keys used here are disposable."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time

import pytest
from nacl.public import PrivateKey

ROOT = Path(__file__).resolve().parents[1]
for folder in ("tools", "app", "deploy/linux"):
    sys.path.insert(0, str(ROOT / folder))
import contact_fixture
import contact_privacy as contact
import contact_ssh
import demo
from staff_admin import write_staff_password

ORIGIN = "https://synthetic.example.invalid"
HEADERS = {"Origin": ORIGIN, "X-Stamp-Client": "1"}
NAME = "합성 연락처"
NUMBER = "010" + "37" * 4
OLD_NUMBER = "011" + "62" * 4
OLD_CARD = "c" * 32


def test_shared_phone_grammar():
    cases = json.loads((ROOT / "app/tests/fixtures/phone-formats.json").read_text())
    for raw, spec in cases["accepted"]:
        assert contact.phone_digits(raw.replace("D", "5")) == spec[:3] + "5" * int(spec[3])
    for raw in cases["rejected"]:
        assert contact.phone_digits(raw.replace("D", "5")) is None


@pytest.fixture
def legacy(tmp_path):
    if os.name != "posix":
        pytest.skip("Linux-compatible state permissions required")
    event = json.loads((ROOT / "config/event.example.json").read_text())
    now = datetime.now(timezone.utc)
    event.update(id="demo-contact-test", draft=False, starts_at=(now-timedelta(days=1)).isoformat(),
                 ends_at=(now+timedelta(days=1)).isoformat(), reward_until=(now+timedelta(days=1)).isoformat())
    config = tmp_path / "event.json"
    config.write_text(json.dumps(event))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    demo.prepare(state, config, ORIGIN)
    db = state / demo.DATABASE_NAME
    write_staff_password(db, "synthetic-staff", secrets.token_urlsafe(24))
    key = demo.validate_database(db, demo.load_demo_event(config))
    with closing(demo.connect_db(db, mode="rw")) as conn, conn:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO customers VALUES (?, 1, 5)", (OLD_CARD,))
        conn.execute("INSERT INTO customer_profiles VALUES (?,?,?,'demo-v1',1)",
                     (OLD_CARD, NAME, hmac.digest(key, OLD_NUMBER.encode(), "sha256").hex()))
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (secrets.token_hex(32), OLD_CARD, 'test', int(time.time())+9999))
        conn.execute("INSERT INTO stamps VALUES (?,'season-01',3)", (OLD_CARD,))
        conn.execute("INSERT INTO recovery VALUES (?,?,?)", (OLD_CARD, secrets.token_hex(32), int(time.time())+9999))
        conn.execute("INSERT INTO reward_audit VALUES (?,5,'unauthenticated_customer_screen')", (OLD_CARD,))
        conn.execute("INSERT INTO stamp_cancel_audit VALUES (1,?,'season-02',2,3,'test-staff')", (OLD_CARD,))
        conn.execute("INSERT INTO admin_sessions VALUES (?,'test','test-staff',1,2)", (secrets.token_hex(32),))
    private = PrivateKey.generate()
    policy = contact_fixture.write_config(contact, tmp_path / "public.json", event["id"], [(NAME, NUMBER)], private.public_key)
    return state, config, db, policy


def migrate_fixture_schema(state, config, origin, policy=None):
    # Populated synthetic fixtures exercise data preservation / fault injection in the
    # transactional migration itself. The public drill entry only accepts empty targets.
    demo.synthetic_origin(origin)
    path = state / demo.DATABASE_NAME
    with closing(demo.connect_db(path, mode="rw")) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        demo._apply_migration(conn, demo.load_demo_event(config), demo.default_profile_key_path(path), policy)


def old_rows(conn):
    return {table: [tuple(row) for row in conn.execute("SELECT * FROM " + table + " ORDER BY 1")]
            for table in demo.TABLES if table != "metadata"}


def test_atomic_migration_preserves_source_failure_retry_and_latest_writes(legacy, monkeypatch):
    state, config, db, policy = legacy
    key_path = demo.default_profile_key_path(db)
    key = key_path.read_bytes()
    signature = demo.inventory_signature
    with closing(demo.connect_db(db, mode="rw")) as conn:
        rows, metadata = old_rows(conn), dict(conn.execute("SELECT * FROM metadata"))
        def injected(connection, event, profile_key):
            if contact.contact_key_id(connection):
                raise RuntimeError("injected before inventory commit")
            return signature(connection, event, profile_key)
        with monkeypatch.context() as patch:
            patch.setattr(demo, "inventory_signature", injected)
            with pytest.raises(RuntimeError):
                migrate_fixture_schema(state, config, ORIGIN, policy)
        assert contact.contact_key_id(conn) is None
        assert rows == old_rows(conn) and metadata == dict(conn.execute("SELECT * FROM metadata"))
        migrate_fixture_schema(state, config, ORIGIN, policy)
        assert rows == old_rows(conn)
        assert all(dict(conn.execute("SELECT * FROM metadata"))[k] == v for k, v in metadata.items())
        assert conn.execute("SELECT count(*) FROM customer_contacts").fetchone()[0] == 0
        conn.execute("INSERT INTO customers VALUES ('after-migration',10,NULL)")
        conn.execute("INSERT INTO stamps VALUES ('after-migration','season-02',11)")
        later = old_rows(conn)
        migrate_fixture_schema(state, config, ORIGIN, policy)
        assert old_rows(conn) == later
        assert key == key_path.read_bytes()
    with pytest.raises(ValueError):
        demo.demo_app(state, config, ORIGIN)
    other = contact_fixture.write_config(contact, state.parent / "other.json", "demo-contact-test", [])
    with pytest.raises(ValueError):
        migrate_fixture_schema(state, config, ORIGIN, other)
    with pytest.raises(ValueError):
        demo.demo_app(state, config, ORIGIN, other)


def post(client, route, payload, headers=None):
    return client.post("/api/" + route, base_url=ORIGIN, json=payload, headers={**HEADERS, **(headers or {})})


def signup():
    return dict(name=NAME, phone=NUMBER, notice_acknowledged=True, notice_version=contact.NOTICE)


def test_policy_and_no_legacy_backfill(legacy, monkeypatch):
    state, config, db, policy = legacy
    client = demo.demo_app(state, config, ORIGIN).test_client()
    assert post(client, "register", signup()).json["code"] == "registration_closed"
    assert post(client, "find", {"name": NAME, "phone": OLD_NUMBER}).status_code == 200
    migrate_fixture_schema(state, config, ORIGIN, policy)
    site = demo.demo_app(state, config, ORIGIN, policy)
    client = site.test_client()
    for fields, code in [({"notice_version": "demo-v1"}, "notice_changed"),
                         ({"name": "미승인 합성"}, "synthetic_only")]:
        assert post(client, "register", {**signup(), **fields}).json["code"] == code
    for route in ("register", "find"):
        assert post(client, route, {**signup(), "phone": NUMBER + "-"}).status_code == 400
    def broken(*_):
        raise sqlite3.OperationalError("synthetic write failure")
    with monkeypatch.context() as patch:
        patch.setattr(contact.SyntheticEnrollment, "store", broken)
        assert post(client, "register", signup()).status_code >= 500
    assert post(client, "find", {"name": NAME, "phone": OLD_NUMBER}).status_code == 200
    with closing(demo.connect_db(db, mode="ro")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM customer_contacts").fetchone()[0] == 0
    assert post(site.test_client(), "register", signup()).status_code == 200
    assert post(site.test_client(), "register", signup()).status_code == 409


def test_lookup_audit_first_no_raw_phone_in_database_staff_or_audit(legacy, monkeypatch):
    state, config, db, policy = legacy
    event = demo.load_demo_event(config)
    migrate_fixture_schema(state, config, ORIGIN, policy)
    site = demo.demo_app(state, config, ORIGIN, policy)
    visitor = site.test_client()
    response = post(visitor, "register", signup())
    assert response.status_code == 200
    assert NUMBER.encode() not in response.data and NUMBER not in str(response.headers)
    with closing(demo.connect_db(db, mode="ro")) as conn:
        cid, sealed, mask = conn.execute("SELECT customer_id,sealed_phone,phone_masked FROM customer_contacts").fetchone()
        assert isinstance(sealed, bytes) and NUMBER.encode() not in sealed and mask == contact.masked(NUMBER)
    actor = "fixture-operator"
    req = dict(customer_id=cid, purpose="inquiry")
    with pytest.raises(ValueError):
        contact_ssh.release(db, event, actor, {**req, "purpose": "promotion"})
    with pytest.raises(ValueError):
        contact_ssh.release(db, event, actor, {**req, "actor": "someone-else"})
    assert contact_ssh.release(db, event, actor, {**req, "customer_id": OLD_CARD}) == {"status": "unavailable"}
    connection = contact_ssh.connect_db
    def deny_audit(path, *, mode):
        conn = connection(path, mode=mode)
        conn.set_authorizer(lambda action, table, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_INSERT and table == "contact_access_audit" else sqlite3.SQLITE_OK)
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(contact_ssh, "connect_db", deny_audit)
        with pytest.raises(sqlite3.DatabaseError):
            contact_ssh.release(db, event, actor, req)
    reply = contact_ssh.release(db, event, actor, req)
    assert reply["status"] == "released" and NUMBER not in json.dumps(reply)
    with pytest.raises(ValueError):
        contact_ssh.acknowledge(db, "different-operator", reply["request_id"], "displayed")
    contact_ssh.acknowledge(db, actor, reply["request_id"], "decrypt_failed")
    with closing(demo.connect_db(db, mode="ro")) as conn:
        history = [tuple(row) for row in conn.execute("SELECT actor,purpose,result FROM contact_access_audit ORDER BY rowid")]
        assert history == [(actor, "inquiry", "unavailable"), (actor, "inquiry", "decrypt_failed")]
        assert all(NUMBER not in line for line in conn.iterdump())
        for path in [db, Path(str(db) + "-wal")]:
            if path.exists():
                assert NUMBER.encode() not in path.read_bytes()
    assert visitor.get("/api/admin/stats", base_url=ORIGIN).status_code == 401
    assert not any("contact" in rule.rule for rule in site.url_map.iter_rules())


def test_password_floor_common_values_and_unchanged_verifier_on_failure(tmp_path):
    db = tmp_path / "fixture.sqlite"
    valid = secrets.token_urlsafe(28)
    write_staff_password(db, "fixture", valid[:15])
    verifier = Path(str(db) + ".admin.key")
    before = verifier.read_bytes()
    for weak in (valid[:14], "1234567890" * 2, "password" + "1" * 9, "Ab!" * 7, "abcdefghijklmnop"):
        with pytest.raises(ValueError):
            write_staff_password(db, "fixture", weak)
    assert verifier.read_bytes() == before



def test_live_recipient_and_synthetic_origin_checked_before_writes(legacy):
    state, config, db, policy = legacy
    with pytest.raises(ValueError):
        migrate_fixture_schema(state, config, "https://public.example.org", policy)
    migrate_fixture_schema(state, config, ORIGIN, policy)
    with pytest.raises(ValueError):
        demo.demo_app(state, config, "https://public.example.org", policy)
    site = demo.demo_app(state, config, ORIGIN, policy)
    with closing(demo.connect_db(db, mode="rw")) as conn:
        conn.execute("UPDATE metadata SET value=? WHERE key='contact_recipient_v1'", (secrets.token_hex(32),))
        response = post(site.test_client(), "register", signup())
        assert response.status_code >= 500
        assert conn.execute("SELECT count(*) FROM customer_contacts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM customers").fetchone()[0] == 1


def test_legacy_staff_lookup_and_direct_phone_validation(legacy):
    state, config, db, policy = legacy
    migrate_fixture_schema(state, config, ORIGIN, policy)
    password = secrets.token_urlsafe(30)
    write_staff_password(db, "fixture-staff", password)
    site = demo.demo_app(state, config, ORIGIN, policy)
    staff = site.test_client()
    csrf = post(staff, "admin/login", {"password": password}).json["csrf"]
    headers = {"X-Admin-CSRF": csrf}
    response = post(staff, "admin/search", {"phone": "+82 " + OLD_NUMBER[1:]}, headers)
    assert response.status_code == 200 and response.json["phone_masked"] is None
    assert response.json["customer_id"] == OLD_CARD and OLD_NUMBER.encode() not in response.data
    invalid = json.loads((ROOT / "app/tests/fixtures/phone-formats.json").read_text())["rejected"]
    visitor = site.test_client()
    for template in invalid:
        number = template.replace("D", "5")
        assert post(visitor, "register", {**signup(), "phone": number}).status_code == 400
        assert post(visitor, "find", {"name": NAME, "phone": number}).status_code == 400
        assert post(staff, "admin/search", {"phone": number}, headers).status_code == 400
    with closing(demo.connect_db(db, mode="ro")) as conn:
        assert conn.execute("SELECT count(*) FROM customer_contacts").fetchone()[0] == 0


def test_password_cli_refuses_pipe_mismatched_confirmation_or_echo_fallback(tmp_path, monkeypatch, request):
    import getpass
    import manage
    import warnings

    previous_umask = os.umask(0o077)
    request.addfinalizer(lambda: os.umask(previous_umask))
    database = tmp_path / "fixture.sqlite"
    monkeypatch.setattr(sys, "argv", ["manage", "--database", str(database), "admin-password", "--actor", "test"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        manage.main()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def cannot_hide(prompt):
        warnings.warn("hidden input unavailable", getpass.GetPassWarning)
    monkeypatch.setattr(getpass, "getpass", cannot_hide)
    with pytest.raises(SystemExit):
        manage.main()
    monkeypatch.setattr(getpass, "getpass", lambda prompt: secrets.token_urlsafe(30))
    with pytest.raises(SystemExit):
        manage.main()
    assert not Path(str(database) + ".admin.key").exists()


@pytest.mark.parametrize("contacts_first", [True, False])
def test_correction_contact_migrations_preserve_both_orders_and_are_idempotent(legacy, contacts_first):
    state, config, path, policy = legacy
    with closing(demo.connect_db(path, mode="ro")) as reader:
        original = old_rows(reader)
        original_metadata = dict(reader.execute("SELECT * FROM metadata"))
        assert not demo.correction_schema.correction_enabled(reader)
        if contacts_first:
            migrate_fixture_schema(state, config, ORIGIN, policy)
        migrate_fixture_schema(state, config, ORIGIN)
        if not contacts_first:
            migrate_fixture_schema(state, config, ORIGIN, policy)
        migrated = dict(reader.execute("SELECT * FROM metadata"))
        assert {k: migrated[k] for k in original_metadata} == original_metadata
        assert "linux_demo_inventory_v3" in migrated
        assert demo.correction_schema.correction_enabled(reader)
        assert contact.contact_key_id(reader) == contact.SyntheticEnrollment.read(policy, "demo-contact-test").key_id
        migrate_fixture_schema(state, config, ORIGIN)
        migrate_fixture_schema(state, config, ORIGIN, policy)
        assert dict(reader.execute("SELECT * FROM metadata")) == migrated
        assert old_rows(reader) == original
        assert reader.execute("PRAGMA foreign_key_check").fetchone() is None
    assert demo.demo_app(state, config, ORIGIN, policy).test_client().get("/health", base_url=ORIGIN).status_code == 200


def test_correction_migration_rollback_and_retry(legacy, monkeypatch):
    state, config, path, _ = legacy
    sign = demo.inventory_signature

    def fail_before_commit(conn, event, key):
        if demo.correction_schema.correction_enabled(conn):
            raise RuntimeError("synthetic failure after DDL")
        return sign(conn, event, key)

    with closing(demo.connect_db(path, mode="ro")) as reader:
        original = old_rows(reader)
        original_metadata = dict(reader.execute("SELECT * FROM metadata"))
        with monkeypatch.context() as patch:
            patch.setattr(demo, "inventory_signature", fail_before_commit)
            with pytest.raises(RuntimeError):
                migrate_fixture_schema(state, config, ORIGIN)
        assert not demo.correction_schema.correction_enabled(reader)
        assert dict(reader.execute("SELECT * FROM metadata")) == original_metadata
        assert old_rows(reader) == original
        migrate_fixture_schema(state, config, ORIGIN)
        assert demo.correction_schema.correction_enabled(reader)
        assert old_rows(reader) == original


@pytest.mark.parametrize("damage", ["qr", "trigger_body", "inventory"])
def test_correction_migration_refuses_to_resign_damaged_source(legacy, damage):
    state, config, path, _ = legacy
    with closing(demo.connect_db(path, mode="rw")) as writer, writer:
        if damage == "qr":
            writer.execute("UPDATE products SET qr_key=? WHERE id='season-01'", (secrets.token_urlsafe(24),))
        elif damage == "trigger_body":
            writer.execute("DROP TRIGGER protect_redeemed_stamp")
            writer.execute("CREATE TRIGGER protect_redeemed_stamp BEFORE DELETE ON stamps BEGIN SELECT 1; END")
        else:
            writer.execute("UPDATE metadata SET value='damaged' WHERE key='linux_demo_inventory_v1'")
    with pytest.raises(ValueError):
        migrate_fixture_schema(state, config, ORIGIN)
    with closing(demo.connect_db(path, mode="ro")) as reader:
        assert not demo.correction_schema.correction_enabled(reader)
        assert reader.execute("SELECT 1 FROM metadata WHERE key='linux_demo_inventory_v3'").fetchone() is None
