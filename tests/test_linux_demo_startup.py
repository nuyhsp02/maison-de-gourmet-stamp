"""Strict startup and synthetic provisioning regressions. All data lives under tmp_path."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from threading import Barrier

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "linux"))
import demo
from staff_admin import staff_key_path, write_staff_password

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux DEMO uses POSIX file permissions")
ORIGIN = "https://demo.example.invalid"


@pytest.fixture
def inputs(tmp_path):
    event = json.loads((ROOT / "config/event.example.json").read_text(encoding="utf-8"))
    # Historical fixture dates deliberately independent of the store event.
    event.update(id="demo-test-archive", draft=False, starts_at="2001-01-01T00:00:00Z",
                 ends_at="2001-02-28T00:00:00Z", reward_until="2001-03-01T00:00:00Z")
    config = tmp_path / "fixture.json"
    config.write_text(json.dumps(event))
    state = tmp_path / "private state #1%?"
    state.mkdir(mode=0o700)
    return state, config


@pytest.fixture(scope="module")
def credentials(tmp_path_factory):
    name = tmp_path_factory.mktemp("fixture-verifier") / demo.DATABASE_NAME
    write_staff_password(name, "synthetic-staff", secrets.token_urlsafe(24))
    return staff_key_path(name).read_bytes()


@pytest.fixture
def ready(inputs, credentials):
    state, config = inputs
    demo.prepare(state, config, ORIGIN)
    database = state / demo.DATABASE_NAME
    admin = staff_key_path(database)
    admin.write_bytes(credentials)
    admin.chmod(0o600)
    return state, config, database


def test_prepare_is_explicit_private_and_independent(inputs, tmp_path):
    state, config = inputs
    dirs = [state, tmp_path / "second"]
    dirs[1].mkdir(mode=0o700)
    inventories, keys = [], []
    for directory in dirs:
        demo.prepare(directory, config, ORIGIN)
        dbpath = directory / demo.DATABASE_NAME
        key = demo.default_profile_key_path(dbpath)
        assert set(directory.iterdir()) == {dbpath, key}
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in (dbpath, key))
        keys.append(key.read_bytes())
        with closing(demo.connect_db(dbpath, mode="ro")) as db:
            inventories.append({r[0] for r in db.execute("SELECT qr_key FROM products")})
            assert len(inventories[-1]) == 10
            assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
        before = dbpath.read_bytes(), key.read_bytes()
        with pytest.raises(demo.DemoError):
            demo.prepare(directory, config, ORIGIN)
        assert (dbpath.read_bytes(), key.read_bytes()) == before
    assert not inventories[0].intersection(inventories[1])
    assert keys[0] != keys[1]


def test_simultaneous_prepare_only_one_complete_pair(inputs):
    state, config = inputs
    gate = Barrier(2)

    def provision():
        gate.wait(timeout=10)
        try:
            demo.prepare(state, config, ORIGIN)
        except (demo.DemoError, FileExistsError):
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(provision) for _ in range(2)]
        assert sorted(f.result(timeout=15) for f in results) == [False, True]
    assert demo.validate_database(state / demo.DATABASE_NAME, demo.load_demo_event(config))


@pytest.mark.parametrize("override", [{"id": "store-live"}, {"draft": True},
                                      {"ends_at": "2002-01-01T00:00:00Z"}])
def test_prepare_refuses_unsuitable_config_without_creating_data(inputs, override):
    state, config = inputs
    event = json.loads(config.read_text())
    config.write_text(json.dumps(event | override))
    with pytest.raises(demo.DemoError):
        demo.prepare(state, config, ORIGIN)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("origin", ["http://demo.example.invalid", ORIGIN + "/q",
                                    ORIGIN + ":443", ORIGIN + ":8443", "https://127.0.0.1",
                                    "https://user@demo.example.invalid",
                                    ORIGIN + "?data", "https://*.example.invalid"])
def test_prepare_rejects_noncanonical_https_before_creating_data(inputs, origin):
    state, config = inputs
    with pytest.raises(ValueError):
        demo.prepare(state, config, origin)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("filename", [demo.DATABASE_NAME, demo.DATABASE_NAME + ".profile.key",
                                      demo.DATABASE_NAME + ".admin.key"])
def test_missing_files_never_recreated(ready, filename):
    state, config, _ = ready
    (state / filename).unlink()
    with pytest.raises(demo.DemoError):
        demo.demo_app(state, config, ORIGIN)
    assert not (state / filename).exists()


@pytest.mark.parametrize("mutation", [
    "DELETE FROM metadata WHERE key='linux_demo_v1'",
    "DELETE FROM metadata WHERE key='profile_key_check_v1'",
    "DELETE FROM metadata WHERE key='admin_schema_v1'",
    "UPDATE metadata SET value='different' WHERE key='event'",
    "DROP TABLE customer_profiles",
    "DROP TRIGGER protect_redeemed_stamp",
    "DROP INDEX sessions_customer",
    "DELETE FROM products WHERE id='select-01'",
    "UPDATE products SET qr_key='bad' WHERE id='select-01'",
    "UPDATE products SET qr_key=lower(hex(randomblob(16))) WHERE id='select-01'",
    "PRAGMA journal_mode=DELETE",
])
def test_refuses_to_repair_existing_data(ready, mutation):
    state, config, database = ready
    with closing(demo.connect_db(database, mode="rw")) as db:
        db.execute(mutation).fetchall()
    before = database.read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        demo.demo_app(state, config, ORIGIN)
    assert database.read_bytes() == before


def test_wrong_profile_key_and_invalid_admin_verifier_stay_untouched(ready):
    state, config, database = ready
    key = demo.default_profile_key_path(database)
    original = key.read_bytes()
    replacement = secrets.token_bytes(32)
    key.write_bytes(replacement)
    with pytest.raises(demo.DemoError):
        demo.demo_app(state, config, ORIGIN)
    assert key.read_bytes() == replacement
    key.write_bytes(original)
    staff = staff_key_path(database)
    staff.write_bytes(b"{}")
    with pytest.raises(demo.DemoError):
        demo.demo_app(state, config, ORIGIN)
    assert staff.read_bytes() == b"{}"


def test_strict_state_requires_private_real_files_and_directory(ready, tmp_path):
    state, config, database = ready
    key = demo.default_profile_key_path(database)
    key.chmod(0o644)
    with pytest.raises(demo.DemoError):
        demo.demo_app(state, config, ORIGIN)
    key.chmod(0o600)
    retained = tmp_path / "original"
    key.rename(retained)
    key.symlink_to(retained)
    with pytest.raises(demo.DemoError):
        demo.demo_app(state, config, ORIGIN)
    assert key.is_symlink()
    state.chmod(0o755)
    with pytest.raises(demo.DemoError):
        demo.demo_app(state, config, ORIGIN)


def test_only_existing_db_is_opened_on_start_and_every_request(ready, monkeypatch):
    state, config, database = ready
    core = sys.modules[demo.create_app.__module__]
    original = demo.connect_db
    modes, statements = [], []

    def observed(path, *, mode):
        modes.append(mode)
        db = original(path, mode=mode)
        db.set_trace_callback(statements.append)
        return db

    def forbid_init(*args, **kwargs):
        pytest.fail("existing DEMO invoked the migration path")

    for module in (core, demo):
        monkeypatch.setattr(module, "init_db", forbid_init)
        monkeypatch.setattr(module, "connect_db", observed)
    app = demo.demo_app(state, config, ORIGIN)
    client = app.test_client()
    assert modes == ["ro"]
    assert client.get("/health", base_url=ORIGIN).status_code == 200
    assert modes == ["ro", "rw"]
    assert not any(q.startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
                   for q in statements)
    database.unlink()
    assert client.get("/health", base_url=ORIGIN).status_code == 503
    assert not database.exists()


def test_requires_https_host_and_write_origin(ready):
    state, config, _ = ready
    client = demo.demo_app(state, config, ORIGIN).test_client()
    assert client.get("/health", base_url="http://demo.example.invalid").json["code"] == "https_required"
    assert client.get("/health", base_url="https://other.example.invalid").json["code"] == "invalid_host"
    res = client.post("/api/register", base_url=ORIGIN, json={}, headers={
        "X-Stamp-Client": "1", "Origin": "https://other.example.invalid"})
    assert res.status_code == 403 and res.json["code"] == "invalid_origin"


def test_journal_formatter_drops_framework_request_details():
    sentinel = secrets.token_urlsafe(24)
    try:
        raise ValueError(sentinel)
    except ValueError:
        record = logging.LogRecord("waitress", logging.ERROR, __file__, 1, "/q/%s",
                                   (sentinel,), sys.exc_info())
    assert demo.JournalFormatter().format(record) == "ERROR waitress"
