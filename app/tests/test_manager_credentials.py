"""Exercise the two local verifier files with disposable credentials."""
from contextlib import closing
import getpass
import json
import os
import secrets
import sqlite3
import stat
import sys

import pytest
from werkzeug.security import check_password_hash

import manage
from conftest import BASE
from staff_admin import MANAGER_BINDING, manager_key_path, staff_key_path, write_staff_password


def authenticate(client, route, password):
    return client.post("/api/admin/" + route, base_url=BASE, json={"password": password},
                       headers={"Origin": BASE, "X-Stamp-Client": "1"})


def session(client):
    return client.get("/api/admin/session", base_url=BASE)


def test_manager_provisioning_uses_hidden_terminal_and_preserves_staff(app, monkeypatch, capsys):
    database = app.config["DATABASE"]
    regular, privileged = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    write_staff_password(database, "counter", regular)
    staff_verifier = staff_key_path(database).read_bytes()
    args = ["manage", "--database", str(database), "manager-password", "--actor", "supervisor"]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    old_mask = os.umask(0o077)
    try:
        with pytest.raises(SystemExit):
            manage.main()
        assert not manager_key_path(database).exists()
        replies = iter((privileged, privileged))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(getpass, "getpass", lambda prompt: next(replies))
        manage.main()
    finally:
        os.umask(old_mask)
    assert next(replies, None) is None
    out, error = capsys.readouterr()
    assert privileged not in out + error and regular not in out + error
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    assert staff_verifier == staff_key_path(database).read_bytes()
    verifier = manager_key_path(database)
    record = json.loads(verifier.read_bytes())
    assert set(record) == {"actor", "password_hash"} and record["actor"] == "supervisor"
    assert check_password_hash(record["password_hash"], privileged) and privileged not in verifier.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(verifier.stat().st_mode) == 0o600
    for role, actor, rejected in (("admin", "COUNTER", secrets.token_urlsafe(24)),
                                  ("admin", "supervisor", regular), ("staff", "counter", privileged)):
        with pytest.raises(ValueError):
            write_staff_password(database, actor, rejected, role=role)
    assert staff_key_path(database).read_bytes() == staff_verifier
    assert json.loads(verifier.read_bytes()) == record


def test_role_rotation_invalidates_only_that_roles_sessions(app):
    database = app.config["DATABASE"]
    regular, privileged = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    write_staff_password(database, "counter", regular)
    write_staff_password(database, "supervisor", privileged, role="admin")
    staff, manager = app.test_client(), app.test_client()
    assert authenticate(staff, "login", regular).json["role"] == "staff"
    assert authenticate(manager, "manager-login", privileged).json["role"] == "admin"
    replacement = secrets.token_urlsafe(24)
    write_staff_password(database, "supervisor", replacement, role="admin")
    assert session(manager).status_code == 401 and session(staff).json["role"] == "staff"
    assert authenticate(manager, "manager-login", replacement).json["role"] == "admin"
    write_staff_password(database, "counter", secrets.token_urlsafe(24))
    assert session(staff).status_code == 401 and session(manager).json["role"] == "admin"


def test_switching_a_browser_back_to_staff_revokes_its_manager_session(app):
    database = app.config["DATABASE"]
    regular, privileged = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    write_staff_password(database, "counter", regular)
    write_staff_password(database, "supervisor", privileged, role="admin")
    browser = app.test_client()
    assert authenticate(browser, "manager-login", privileged).json["role"] == "admin"
    old_cookie = browser.get_cookie("stamp_admin_dev", domain="127.0.0.1")
    assert authenticate(browser, "login", regular).json["role"] == "staff"
    old_session = app.test_client()
    old_session.set_cookie(old_cookie.key, old_cookie.value, domain=old_cookie.domain)
    assert session(old_session).status_code == 401 and session(browser).json["role"] == "staff"


def test_missing_or_accidentally_duplicated_manager_verifier_never_elevates_staff(app):
    database = app.config["DATABASE"]
    regular = secrets.token_urlsafe(24)
    write_staff_password(database, "counter", regular)
    client = app.test_client()
    assert authenticate(client, "manager-login", regular).status_code == 503
    source = staff_key_path(database)
    manager = manager_key_path(database)
    record = json.loads(source.read_text())
    record["actor"] = "supervisor"
    manager.write_text(json.dumps(record))
    manager.chmod(0o600)
    assert authenticate(client, "manager-login", regular).status_code == 503
    assert session(client).status_code == 401
    assert authenticate(client, "login", regular).json["role"] == "staff"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_manager_verifier_requires_private_regular_file(app):
    database = app.config["DATABASE"]
    privileged = secrets.token_urlsafe(24)
    write_staff_password(database, "supervisor", privileged, role="admin")
    manager = manager_key_path(database)
    client = app.test_client()
    assert authenticate(client, "manager-login", privileged).json["role"] == "admin"
    manager.chmod(0o644)
    assert session(client).status_code == 401
    assert authenticate(client, "manager-login", privileged).status_code == 503
    alternate = manager.with_name("alternative.key")
    manager.rename(alternate)
    alternate.chmod(0o600)
    manager.symlink_to(alternate)
    assert session(client).status_code == 401
    assert authenticate(client, "manager-login", privileged).status_code == 503


def test_manager_provision_fails_closed_for_missing_or_uninitialized_db(tmp_path):
    database = tmp_path / "missing.sqlite"
    password = secrets.token_urlsafe(24)
    with pytest.raises(ValueError):
        write_staff_password(database, "supervisor", password, role="admin")
    assert not database.exists() and not manager_key_path(database).exists()
    database.touch()
    with pytest.raises(ValueError):
        write_staff_password(database, "supervisor", password, role="admin")
    assert database.stat().st_size == 0 and not manager_key_path(database).exists()


def test_rotation_refuses_same_password_and_restored_old_verifier(app):
    db_path = app.config["DATABASE"]
    old, new = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    write_staff_password(db_path, "first-manager", old, role="admin")
    old_session = app.test_client()
    assert authenticate(old_session, "manager-login", old).status_code == 200
    path = manager_key_path(db_path)
    previous = path.read_bytes()
    with pytest.raises(ValueError):
        write_staff_password(db_path, "first-manager", old, role="admin")
    assert session(old_session).status_code == 200
    write_staff_password(db_path, "second-manager", new, role="admin")
    current = path.read_bytes()
    login = app.test_client()
    assert authenticate(login, "manager-login", old).status_code == 401
    path.write_bytes(previous)
    assert session(old_session).status_code == 401
    assert authenticate(login, "manager-login", old).status_code == 503
    path.write_bytes(current)
    assert authenticate(login, "manager-login", new).json["role"] == "admin"
    assert session(old_session).status_code == 401


def test_revoke_cli_closes_all_manager_sessions_without_affecting_staff(app, monkeypatch, capsys):
    database = app.config["DATABASE"]
    staff_password, manager_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    write_staff_password(database, "staff", staff_password)
    write_staff_password(database, "supervisor", manager_password, role="admin")
    path = manager_key_path(database)
    previous = path.read_bytes()
    staff = app.test_client()
    assert authenticate(staff, "login", staff_password).status_code == 200
    managers = [app.test_client() for _ in range(2)]
    for manager in managers:
        assert authenticate(manager, "manager-login", manager_password).status_code == 200
    monkeypatch.setattr(sys, "argv", ["manage", "--database", str(database), "manager-revoke"])
    original_mask = os.umask(0o077)
    try:
        manage.main()
    finally:
        os.umask(original_mask)
    assert not path.exists()
    out, err = capsys.readouterr()
    assert staff_password not in out + err and manager_password not in out + err
    path.write_bytes(previous)
    path.chmod(0o600)
    assert authenticate(app.test_client(), "manager-login", manager_password).status_code == 503
    assert all(session(manager).status_code == 401 for manager in managers)
    assert session(staff).json["role"] == "staff"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT admin_actor FROM admin_sessions").fetchall() == [("staff",)]
    fresh = secrets.token_urlsafe(24)
    write_staff_password(database, "new-manager", fresh, role="admin")
    assert authenticate(app.test_client(), "manager-login", manager_password).status_code == 401
    assert authenticate(app.test_client(), "manager-login", fresh).json["role"] == "admin"


def test_binding_mismatch_disables_only_manager(app):
    database = app.config["DATABASE"]
    regular, privileged = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    write_staff_password(database, "staff", regular)
    write_staff_password(database, "supervisor", privileged, role="admin")
    manager, staff = app.test_client(), app.test_client()
    assert authenticate(manager, "manager-login", privileged).status_code == 200
    assert authenticate(staff, "login", regular).status_code == 200
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE metadata SET value=? WHERE key=?", (secrets.token_hex(32), MANAGER_BINDING))
    assert session(manager).status_code == 401
    assert authenticate(manager, "manager-login", privileged).status_code == 503
    assert session(staff).json["role"] == "staff"
    assert staff.get("/api/state", base_url=BASE).status_code == 200


def test_restarting_application_expires_manager_without_disabling_active_credential(app):
    from app import create_app
    from pathlib import Path

    database = app.config["DATABASE"]
    password = secrets.token_urlsafe(24)
    write_staff_password(database, "supervisor", password, role="admin")
    manager = app.test_client()
    assert authenticate(manager, "manager-login", password).status_code == 200
    cookie = manager.get_cookie("stamp_admin_dev", domain="127.0.0.1")
    restarted = create_app(database=database, public_url=BASE, dev=True, testing=True,
                           synthetic_config=Path(database).parent / "synthetic-contact.json")
    client = restarted.test_client()
    client.set_cookie(cookie.key, cookie.value, domain=cookie.domain)
    assert session(client).status_code == 401
    assert authenticate(client, "manager-login", password).json["role"] == "admin"


def test_interrupted_manager_rotation_stays_disabled_until_reissued(app, monkeypatch):
    import staff_admin

    database = app.config["DATABASE"]
    old, interrupted, fresh = (secrets.token_urlsafe(24) for _ in range(3))
    write_staff_password(database, "supervisor", old, role="admin")
    manager = app.test_client()
    assert authenticate(manager, "manager-login", old).status_code == 200
    save = staff_admin.save_verifier

    def fail_after_replace(path, record):
        save(path, record)
        raise OSError("simulated interruption before SQLite commit")

    with monkeypatch.context() as patch:
        patch.setattr(staff_admin, "save_verifier", fail_after_replace)
        with pytest.raises(OSError):
            write_staff_password(database, "supervisor", interrupted, role="admin")
    assert session(manager).status_code == 401
    assert authenticate(manager, "manager-login", interrupted).status_code == 503
    write_staff_password(database, "supervisor", fresh, role="admin")
    assert session(manager).status_code == 401
    assert authenticate(manager, "manager-login", old).status_code == 401
    assert authenticate(manager, "manager-login", fresh).json["role"] == "admin"
