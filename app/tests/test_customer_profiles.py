"""Registration, recovery and additive migration using isolated, synthetic data."""
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from werkzeug.serving import make_server

from app import ROOT, connect_db, create_app, digest
from conftest import BASE, Visitor


NAME = "Profile Test Visitor"
PHONE = "010-0000-0081"
NUMBER = "01000000081"
PROFILE = {"name": NAME, "phone": PHONE}
REGISTER = {**PROFILE, "notice_acknowledged": True}


def row_counts(app, *tables):
    with closing(connect_db(app.config["DATABASE"])) as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables)


@pytest.mark.parametrize("overrides", [
    {"notice_acknowledged": False}, {"notice_acknowledged": 1}, {"notice_acknowledged": "true"},
    {"name": " "}, {"name": None}, {"name": "A" * 81},
    {"phone": "010-12"}, {"phone": "010-0000-0081x"}, {"phone": None},
])
def test_invalid_registration_never_creates_card_or_cookie(app, overrides):
    visitor = Visitor(app, start=False)
    response = visitor.post("/api/register", {**REGISTER, **overrides})
    assert response.status_code == 400
    assert "Set-Cookie" not in response.headers
    assert row_counts(app, "customers", "customer_profiles", "sessions") == (0, 0, 0)


def test_notice_required_and_legacy_session_cannot_create_anonymous_card(app):
    visitor = Visitor(app, start=False)
    assert visitor.post("/api/register", PROFILE).status_code == 400
    assert visitor.post("/api/session", {}).json["code"] == "registration_required"
    assert row_counts(app, "customers", "customer_profiles", "sessions") == (0, 0, 0)


def test_normalized_duplicate_and_wrong_name_do_not_issue_session_or_overwrite(app):
    subscriber = Visitor(app, start=False)
    start = int(time.time())
    assert subscriber.post("/api/register", {**REGISTER, "name": "  " + NAME + "  "}).status_code == 200
    stranger = Visitor(app, start=False)
    for name, phone in [(NAME, NUMBER), ("Different Test Visitor", "+82 10 0000 0081")]:
        response = stranger.post("/api/register", {**REGISTER, "name": name, "phone": phone})
        assert (response.status_code, response.json["code"]) == (409, "phone_registered")
        assert "Set-Cookie" not in response.headers
    wrong_name = stranger.post("/api/find", {**PROFILE, "name": "Different Test Visitor"})
    missing = stranger.post("/api/find", {**PROFILE, "phone": "01000000082"})
    assert wrong_name.status_code == missing.status_code == 404
    assert wrong_name.json == missing.json
    assert wrong_name.json["code"] == "profile_not_found"
    assert stranger.read()["customer"] is None
    assert row_counts(app, "customers", "customer_profiles", "sessions") == (1, 1, 1)
    with closing(connect_db(app.config["DATABASE"])) as db:
        profile = db.execute("SELECT * FROM customer_profiles").fetchone()
        assert profile["display_name"] == NAME
        assert profile["notice_version"]
        assert start <= profile["notice_acknowledged_at"] <= int(time.time())
        key = Path(app.config["DATABASE"] + ".profile.key").read_bytes()
        assert profile["phone_lookup_hmac"] == hmac.digest(key, NUMBER.encode("ascii"), "sha256").hex()
        dump = "\n".join(db.iterdump())
        assert NUMBER not in dump and PHONE not in dump and key.hex() not in dump


def test_two_app_instances_racing_for_one_phone_create_one_card(app):
    # Separate application locks force the uniqueness guarantee to live in SQLite.
    second_app = create_app(database=app.config["DATABASE"], public_url=BASE, dev=True, testing=True)
    barrier = threading.Barrier(2)

    def register(server, phone):
        visitor = Visitor(server, start=False)
        barrier.wait(timeout=10)
        return visitor.post("/api/register", {**REGISTER, "phone": phone})

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(register, server, phone) for server, phone in
                    [(app, PHONE), (second_app, NUMBER)]]
        responses = [request.result(timeout=20) for request in requests]
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert sum("Set-Cookie" in r.headers for r in responses) == 1
    assert row_counts(app, "customers", "customer_profiles", "sessions") == (1, 1, 1)
    with closing(connect_db(app.config["DATABASE"])) as db:
        indexes = db.execute("PRAGMA index_list(customer_profiles)").fetchall()
        unique_columns = [[r["name"] for r in db.execute("SELECT name FROM pragma_index_info(?)",
                                                          (idx["name"],))]
                          for idx in indexes if idx["unique"]]
        assert ["phone_lookup_hmac"] in unique_columns


def test_registration_rolls_back_customer_and_profile_if_session_write_fails(app):
    # A missing audit destination injects an OperationalError on the final insert.
    with closing(connect_db(app.config["DATABASE"])) as db:
        db.execute("""CREATE TRIGGER fail_session BEFORE INSERT ON sessions BEGIN
                      INSERT INTO missing_fixture_table VALUES (1); END""")
    visitor = Visitor(app, start=False)
    response = visitor.post("/api/register", REGISTER)
    assert response.status_code == 503 and "Set-Cookie" not in response.headers
    assert row_counts(app, "customers", "customer_profiles", "sessions") == (0, 0, 0)
    with closing(connect_db(app.config["DATABASE"])) as db:
        db.execute("DROP TRIGGER fail_session")
    assert visitor.post("/api/register", REGISTER).status_code == 200
    assert row_counts(app, "customers", "customer_profiles", "sessions") == (1, 1, 1)


def test_active_session_cannot_switch_identity_or_register_again(app):
    visitor = Visitor(app, start=False)
    assert visitor.post("/api/register", REGISTER).status_code == 200
    original = visitor.cookie()
    visitor.read()
    for path, data in [("/api/register", REGISTER), ("/api/find", PROFILE)]:
        invalid = visitor.client.post(path, json=data, base_url=BASE,
                                      headers={"Origin": BASE, "X-Stamp-Client": "1"})
        assert invalid.status_code == 403
        response = visitor.post(path, data)
        assert response.status_code == 409 and response.json["code"] == "active_session"
    assert visitor.cookie() == original
    assert row_counts(app, "customers", "sessions") == (1, 1)


def test_two_browsers_and_deleted_cookie_share_stamps_and_one_reward(app, keys):
    first = Visitor(app, start=False)
    assert first.post("/api/register", REGISTER).status_code == 200
    first.read()
    assert first.collect(keys[0])["added"]
    assert first.read()["customer"]["count"] == 1
    second = Visitor(app, start=False)
    assert second.read()["customer"] is None
    recovered = second.post("/api/find", {**PROFILE, "phone": "+82 10 0000 0081"})
    assert recovered.status_code == 200
    assert second.read()["customer"] == first.read()["customer"]
    assert first.cookie() != second.cookie()
    assert second.collect(keys[0])["added"] is False
    for key in keys[1:]:
        assert second.collect(key)["added"]
    assert first.read()["customer"]["status"] == "complete"
    assert first.post("/api/redeem", {"confirm_received": True}).json["already_redeemed"] is False
    assert second.post("/api/redeem", {"confirm_received": True}).json["already_redeemed"] is True
    previous = first.cookie()
    first.client.delete_cookie(app.cookie_name, domain=first.domain)
    assert first.read()["customer"] is None
    assert first.post("/api/find", PROFILE).status_code == 200
    assert first.cookie() != previous
    assert first.read()["customer"] == second.read()["customer"]
    assert first.state["customer"]["status"] == "redeemed"
    with closing(connect_db(app.config["DATABASE"])) as db:
        assert db.execute("SELECT COUNT(DISTINCT customer_id) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM reward_audit").fetchone()[0] == 1


def test_rate_limits_apply_to_normalized_phone_across_registration_and_find(tmp_path):
    app = create_app(database=tmp_path / "limited.sqlite", public_url=BASE, dev=True)
    visitor = Visitor(app, start=False)
    for i in range(10):
        response = visitor.post("/api/find", {**PROFILE, "phone": PHONE if i % 2 else NUMBER})
        assert response.status_code == 404
    response = visitor.post("/api/register", REGISTER)
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"
    assert row_counts(app, "customers", "sessions") == (0, 0)


@pytest.mark.skipif(os.environ.get("MAISON_HTTP_TESTS") != "1", reason="Explicit loopback HTTP test opt-in")
def test_registration_and_recovery_only_show_name_in_own_card_and_never_log_pii(app, caplog):
    # Capture real HTTP access lines as well as Flask's logger.
    caplog.set_level(logging.INFO)
    host = urlsplit(BASE).netloc
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    cookie = None
    try:
        for path, method, data, status in [
            ("/api/register", "POST", REGISTER, 200), ("/api/register", "POST", REGISTER, 409),
            ("/api/find", "POST", {**PROFILE, "name": "Mismatched Visitor"}, 404),
            ("/api/find", "POST", PROFILE, 200), ("/api/state", "GET", None, 200),
        ]:
            headers = {"Host": host, "Origin": BASE, "X-Stamp-Client": "1", "Content-Type": "application/json"}
            if method == "GET":
                assert cookie is not None
                headers["Cookie"] = cookie
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as conn:
                conn.request(method, path, json.dumps(data) if data is not None else None, headers)
                response = conn.getresponse()
                assert response.status == status
                payload = json.load(response)
                if method == "GET":
                    assert payload["customer"]["registered"] is True
                    assert response.getheader("Cache-Control") == "no-store"
                    assert payload["customer"].pop("display_name") == NAME
                if response.getheader("Set-Cookie"):
                    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                assert response.getheader("Location") is None
                visible = str(response.getheaders()) + str(payload)
                for value in (NAME, NUMBER, PHONE, "Mismatched Visitor"):
                    assert value not in visible
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
    for path in ["/api/register", "/api/find", "/api/state"]:
        assert path + " HTTP" in caplog.text
    for value in (NAME, NUMBER, PHONE, "Mismatched Visitor"):
        assert value not in caplog.text


def test_display_name_follows_the_authenticated_card(app):
    owner = Visitor(app, start=False)
    assert owner.post("/api/register", REGISTER).status_code == 200
    assert owner.read()["customer"]["display_name"] == NAME

    other = Visitor(app, start=False)
    assert other.read()["customer"] is None
    assert other.post("/api/register", {**REGISTER, "name": "별도 합성", "phone": "01000000082"}).status_code == 200
    assert other.read()["customer"]["display_name"] == "별도 합성"
    assert NAME not in json.dumps(other.state)

    recovered = Visitor(app, start=False)
    assert recovered.post("/api/find", PROFILE).status_code == 200
    assert recovered.read()["customer"]["display_name"] == NAME


def test_additive_migration_preserves_all_legacy_rows_and_is_idempotent(tmp_path):
    database = tmp_path / "anonymous.sqlite"
    event = json.loads((ROOT / "event.json").read_text(encoding="utf-8"))
    products = sorted(p["id"] for p in event["products"])
    # Frozen before profiles existed. Every old table, including reward_audit,
    # has at least one pre-existing row.
    with closing(connect_db(database)) as db:
        db.executescript((ROOT / "tests/fixtures/v1-schema.sql").read_text())
        db.execute("INSERT INTO metadata VALUES ('event', ?)", (json.dumps([event["id"], products]),))
        db.execute("INSERT INTO metadata VALUES ('fixture-marker', 'keep')")
        for i, product in enumerate(products):
            db.execute("INSERT INTO products VALUES (?, ?)", (product, f"fixture-qr-{i}"))
        db.execute("INSERT INTO customers VALUES ('used-card', 10, 25)")
        db.execute("INSERT INTO sessions VALUES (?, 'used-card', 'old-csrf', ?)",
                   (digest("c" * 43), int(time.time()) + 3600))
        db.execute("INSERT INTO recovery VALUES ('used-card', ?, ?)",
                   (digest("r" * 43), int(time.time()) + 3600))
        db.executemany("INSERT INTO stamps VALUES ('used-card', ?, 20)", [(p,) for p in products])
        db.execute("INSERT INTO reward_audit VALUES ('used-card', 25, 'unauthenticated_customer_screen')")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        old_rows = {table: {tuple(r) for r in db.execute(f"SELECT * FROM {table}")} for table in tables}
        assert len(old_rows) == 7

    first_dump = None
    for _ in range(2):
        app = create_app(database=database, public_url=BASE, dev=True, testing=True)
        old = Visitor(app, cookie="c" * 43).read()["customer"]
        assert old["display_name"] is None
        assert old["registered"] is False and old["status"] == "redeemed" and old["count"] == 10
        with closing(connect_db(database)) as db:
            for table, expected in old_rows.items():
                actual = {tuple(r) for r in db.execute(f"SELECT * FROM {table}")}
                if table == "metadata":
                    assert expected <= actual  # The HMAC key fingerprint is additive.
                else:
                    assert actual == expected
            assert db.execute("SELECT COUNT(*) FROM customer_profiles").fetchone()[0] == 0
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            dump = list(db.iterdump())
            if first_dump is not None:
                assert dump == first_dump
            first_dump = dump


def test_restored_db_requires_matching_lookup_key_and_recovers_original_card(app, keys, tmp_path):
    owner = Visitor(app, start=False)
    assert owner.post("/api/register", REGISTER).status_code == 200
    owner.read()
    owner.collect(keys[0])
    backup = tmp_path / "restored.sqlite"
    with closing(connect_db(app.config["DATABASE"])) as source, closing(connect_db(backup)) as target:
        source.backup(target)
        baseline = list(target.iterdump())
    backup_key = Path(str(backup) + ".profile.key")
    for contents in (None, os.urandom(32), b"too-short"):
        if contents is not None:
            backup_key.write_bytes(contents)
        with pytest.raises(ValueError):
            create_app(database=backup, public_url=BASE, dev=True, testing=True)
        if contents is None:
            assert not backup_key.exists()
        with closing(connect_db(backup)) as db:
            assert list(db.iterdump()) == baseline
    backup_key.write_bytes(Path(app.config["DATABASE"] + ".profile.key").read_bytes())
    restored = create_app(database=backup, public_url=BASE, dev=True, testing=True)
    visitor = Visitor(restored, start=False)
    assert visitor.post("/api/find", PROFILE).status_code == 200
    assert visitor.read()["customer"] == owner.read()["customer"]
    with closing(connect_db(backup)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [r[0] for r in db.execute("SELECT qr_key FROM products ORDER BY id")] == keys


def test_mismatched_legacy_event_rolls_back_schema_and_preserves_original(tmp_path):
    database = tmp_path / "wrong-event.sqlite"
    with closing(connect_db(database)) as db:
        db.executescript((ROOT / "tests/fixtures/v1-schema.sql").read_text())
        db.execute("INSERT INTO metadata VALUES ('event', 'another-event')")
        db.execute("INSERT INTO customers VALUES ('old-card', 1, NULL)")
        original = list(db.iterdump())
    with pytest.raises(ValueError):
        create_app(database=database, public_url=BASE, dev=True, testing=True)
    assert not Path(str(database) + ".profile.key").exists()
    with closing(connect_db(database)) as db:
        assert list(db.iterdump()) == original


def test_migration_refuses_to_regenerate_missing_fixed_qr(app):
    path = app.config["DATABASE"]
    with closing(connect_db(path)) as db:
        db.execute("DELETE FROM products WHERE id=(SELECT MIN(id) FROM products)")
        before = list(db.iterdump())
    with pytest.raises(ValueError, match="QR"):
        create_app(database=path, public_url=BASE, dev=True, testing=True)
    with closing(connect_db(path)) as db:
        assert list(db.iterdump()) == before


def test_marked_db_missing_profiles_refuses_start_instead_of_recreating_empty_table(app):
    visitor = Visitor(app, start=False)
    assert visitor.post("/api/register", REGISTER).status_code == 200
    database = app.config["DATABASE"]
    key_path = Path(database + ".profile.key")
    original_key = key_path.read_bytes()
    with closing(connect_db(database)) as db:
        # An incomplete restore can pass SQLite's physical integrity check.
        db.execute("DROP TABLE customer_profiles")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT 1 FROM metadata WHERE key='profile_key_check_v1'").fetchone()
        original_dump = list(db.iterdump())
    with pytest.raises(ValueError):
        create_app(database=database, public_url=BASE, dev=True, testing=True)
    assert key_path.read_bytes() == original_key
    with closing(connect_db(database)) as db:
        assert list(db.iterdump()) == original_dump
