"""March 31 boundaries with independent clocks and disposable credentials only."""
from contextlib import closing
from datetime import datetime, timezone
import os
import secrets

import pytest

from lifecycle_fixtures import authority_for
from prod_fixtures import ORIGIN, prod, ready_state
import prod_contact_ssh as operator

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux PROD privacy contract")
HEADERS = {"Origin": ORIGIN, "X-Stamp-Client": "1"}


@pytest.fixture
def site(tmp_path, monkeypatch):
    state, config, _, recipient = ready_state(tmp_path)
    event = prod.load_prod_event(config)
    database = prod.database_path(state)
    authority = authority_for(state, config)
    cutoff = authority.binding[2]
    clock = [cutoff - 1]
    monkeypatch.setattr(prod.time, "time", lambda: clock[0])
    monkeypatch.setenv("MAISON_PROD_LIVE_STATE", str(state))
    cid, token = secrets.token_hex(16), secrets.token_urlsafe(32)
    with closing(prod.application.connect_db(database, mode="rw")) as db, db:
        db.execute("INSERT INTO customers VALUES (?, ?, NULL)", (cid, cutoff - 10))
        db.execute("INSERT INTO customer_profiles VALUES (?, 'Fixture Name', 'fixture-hmac', 'fixture', ?)",
                   (cid, cutoff - 10))
        db.execute("INSERT INTO sessions VALUES (?, ?, 'fixture-csrf', ?)",
                   (prod.application.digest(token), cid, cutoff + 60))
    app = prod.prod_app(state, config, ORIGIN, recipient, authority=authority)
    client = app.test_client()
    client.set_cookie(app.cookie_name, token, domain="store.example.invalid")
    return state, config, recipient, event, authority, clock, app, client, cid


def test_open_customer_and_staff_screens_use_the_same_server_deadline(site):
    _, _, _, _, authority, clock, _, client, _ = site
    for route in ("/", "/admin"):
        page = client.get(route, base_url=ORIGIN)
        assert page.status_code == 200
        assert 'src="/static/privacy-screen.js" data-remaining-ms="1000"' in page.text
    clock[0] = authority.binding[2]
    for route in ("/", "/admin"):
        ended = client.get(route, base_url=ORIGIN)
        assert "privacy-screen.js" not in ended.text and "<form" not in ended.text


def test_all_personal_routes_close_at_exact_kst_with_existing_sessions(site):
    state, config, _, _, authority, clock, app, customer, _ = site
    cutoff = authority.binding[2]
    assert datetime.fromtimestamp(cutoff, timezone.utc).isoformat() == "2027-03-30T15:00:00+00:00"
    assert customer.get("/api/state", base_url=ORIGIN).json["customer"]["display_name"] == "Fixture Name"
    staff = app.test_client()
    manager = app.test_client()
    for role, client in (("staff", staff), ("admin", manager)):
        password = secrets.token_urlsafe(24)
        prod.staff_admin.write_staff_password(prod.database_path(state), "fixture-" + role, password,
                                             role=role, production=True)
        path = "/api/admin/login" if role == "staff" else "/api/admin/manager-login"
        assert client.post(path, base_url=ORIGIN, headers=HEADERS, json={"password":password}).status_code == 200
        assert client.get("/api/admin/stats", base_url=ORIGIN).status_code == 200
    clock[0] = cutoff
    for client, method, path in (
            (customer, "GET", "/api/state"), (customer, "POST", "/api/session"),
            (customer, "POST", "/api/register"), (customer, "POST", "/api/find"),
            (customer, "POST", "/api/collect"), (customer, "POST", "/api/redeem"),
            (staff, "GET", "/api/admin/stats"), (staff, "GET", "/api/admin/session"),
            (staff, "POST", "/api/admin/search"), (staff, "POST", "/api/admin/cancel"),
            (manager, "POST", "/api/admin/correct"), (manager, "POST", "/api/admin/manager-login")):
        response = client.open(path, method=method, base_url=ORIGIN,
                               headers={**HEADERS, "Date": "Wed, 30 Dec 2026 00:00:00 GMT"}, json={})
        assert response.status_code == 410 and response.json["code"] == "privacy_closed", path
        assert "Fixture Name" not in response.text and "WWW-Authenticate" not in response.headers
        assert all("Max-Age=0" in cookie for cookie in response.headers.getlist("Set-Cookie"))
        assert len(response.headers.getlist("Set-Cookie")) == 2
    assert authority.closed
    for route in ("/", "/q/synthetic"):
        notice = customer.get(route, base_url=ORIGIN)
        assert notice.status_code == 200 and "행사가 종료되었습니다" in notice.text
        assert "<form" not in notice.text and "Fixture Name" not in notice.text
    assert customer.get("/recover", base_url=ORIGIN).status_code == 404
    assert customer.get("/static/style.css", base_url=ORIGIN).status_code == 200
    clock[0] = cutoff - 120
    assert customer.get("/api/state", base_url=ORIGIN).status_code == 410
    assert not authority.purged  # API denial is independent of the purge job.


def test_missing_or_unapproved_custody_closes_runtime_and_blocks_boot(site, monkeypatch):
    state, config, recipient, _, authority, _, _, client, _ = site
    authority.ready = False
    for available in (True, False):
        authority.available = available
        response = client.get("/api/state", base_url=ORIGIN)
        assert response.status_code == 503 and response.json["code"] == "privacy_unavailable"
        assert "Fixture Name" not in response.text
        with pytest.raises(ValueError):
            prod.prod_app(state, config, ORIGIN, recipient, authority=authority)
    monkeypatch.delenv("MAISON_PROD_AUTHORITY", raising=False)
    with pytest.raises(ValueError):
        prod.prod_app(state, config, ORIGIN, recipient)


def test_external_closure_and_boot_purge_after_service_was_offline(site):
    state, config, recipient, _, authority, clock, _, client, _ = site
    authority.clock = lambda: authority.binding[2]
    assert client.get("/api/state", base_url=ORIGIN).status_code == 410
    authority.purge_authorized = False
    with pytest.raises(ValueError):
        prod.prod_app(state, config, ORIGIN, recipient, authority=authority)
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    authority.purge_authorized = True
    for _ in range(2):
        restarted = prod.prod_app(state, config, ORIGIN, recipient, authority=authority)
        assert restarted.test_client().get("/api/state", base_url=ORIGIN).status_code == 410
        assert "행사가 종료되었습니다" in restarted.test_client().get("/", base_url=ORIGIN).text
        with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
            assert all(db.execute("SELECT count(*) FROM "+table).fetchone()[0] == 0
                       for table in ("customers", "sessions", "admin_sessions"))
    assert authority.purged and not authority.backups_expired
    with pytest.raises(ValueError):
        prod.check(state, config, ORIGIN, recipient, authority=authority)
    authority.backups_expired = True
    prod.check(state, config, ORIGIN, recipient, authority=authority)


def test_lookup_cannot_release_contact_or_record_access_at_cutoff(site):
    state, _, recipient, event, authority, clock, _, _, cid = site
    database = prod.database_path(state)
    access = prod.PrivacyAccess(state, event, recipient, authority)
    with closing(prod.application.connect_db(database, mode="rw")) as db:
        recipient.store(db, cid, "010" + "0"*8, clock[0])
        clock[0] = authority.binding[2]
        with pytest.raises(ValueError):
            operator.release(db, event, recipient, prod.application.default_profile_key_path(database),
                             "fixture-operator", {"purpose":"pickup","customer_id":cid}, access=access)
        assert db.execute("SELECT count(*) FROM contact_access_audit").fetchone()[0] == 0


def test_read_that_straddles_cutoff_does_not_release_response(site, monkeypatch):
    _, _, _, _, authority, clock, app, client, _ = site
    original = app.view_functions["state"]
    def straddle():
        response = original()
        assert "Fixture Name" in response.get_data(as_text=True)
        clock[0] = authority.binding[2]
        return response
    monkeypatch.setitem(app.view_functions, "state", straddle)
    assert client.get("/api/state", base_url=ORIGIN).json["code"] == "privacy_closed"


def test_db_write_crossing_cutoff_is_rolled_back(site, monkeypatch):
    state, _, _, _, authority, clock, app, _, _ = site
    database = prod.database_path(state)
    pw = secrets.token_urlsafe(24)
    prod.staff_admin.write_staff_password(database, "fixture-shift", pw, production=True)
    # Advance the synthetic clock after the INSERT but before the commit guard.
    original = prod.application.connect_db
    def connection(*args, **kwargs):
        db = original(*args, **kwargs)
        def trace(statement):
            if statement.startswith("INSERT INTO admin_sessions"):
                clock[0] = authority.binding[2]
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(prod.application, "connect_db", connection)
    result = app.test_client().post("/api/admin/login", base_url=ORIGIN, headers=HEADERS, json={"password":pw})
    assert result.status_code == 410
    with closing(original(database, mode="ro")) as db:
        assert db.execute("SELECT count(*) FROM admin_sessions").fetchone()[0] == 0


def test_copy_cannot_report_live_erasure(site, monkeypatch):
    state, config, recipient, _, authority, clock, _, _, _ = site
    clock[0] = authority.binding[2]
    monkeypatch.setenv("MAISON_PROD_LIVE_STATE", str(state.parent))
    with pytest.raises(ValueError):
        prod.prod_app(state, config, ORIGIN, recipient, authority=authority)
    assert not authority.purged


def test_purge_scheduler_uses_kst_equivalent_and_retires_web_process():
    deploy = prod.APP_DIR.parent / "deploy/linux"
    timer = (deploy / "maison-prod-purge.timer").read_text()
    service = (deploy / "maison-prod-purge.service").read_text()
    assert "OnCalendar=2027-03-30 15:00:00 UTC" in timer and "Persistent=true" in timer
    assert "Conflicts=maison-prod.service" in service and "OnSuccess=maison-prod.service" in service
    assert "Restart=on-failure" in service and "--event-id ${PROD_EVENT_ID}" in service


def test_unclassified_live_copy_blocks_erasure_and_completion(site):
    state, _, recipient, event, authority, clock, _, _, _ = site
    clock[0] = authority.binding[2]
    (state / "unclassified-fixture").touch()
    access = prod.PrivacyAccess(state, event, recipient, authority)
    with pytest.raises(ValueError):
        prod.run_purge(state, event, recipient, access)
    assert not authority.purged
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 1


def test_mismatched_key_cannot_mark_either_host_closed(site):
    state, config, recipient, _, authority, clock, _, _, _ = site
    clock[0] = authority.binding[2]
    key = prod.application.default_profile_key_path(prod.database_path(state))
    key.write_bytes(secrets.token_bytes(32))
    with pytest.raises(ValueError):
        prod.prod_app(state, config, ORIGIN, recipient, authority=authority)
    assert not authority.closed and not authority.purged
    assert not (state / ".privacy-closed").exists()
