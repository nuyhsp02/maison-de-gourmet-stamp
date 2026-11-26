"""PROD HTTP enrollment, using only newly generated fixture records and key pairs."""
from contextlib import closing
from datetime import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading

import pytest

from lifecycle_fixtures import authority_for, before_privacy_cutoff
from nacl.public import PrivateKey, SealedBox

from prod_fixtures import ORIGIN, make_config, prod, ready_state


pytestmark = pytest.mark.usefixtures("before_privacy_cutoff")

EVENT = "prod-synthetic-fixture"
NOTICE = "prod-fixture-v1"
NAME = "Synthetic profile"
NUMBER = "010" + "94" * 4
REGISTER = {"notice_acknowledged": True, "notice_version": NOTICE, "name": NAME, "phone": NUMBER}
HEADERS = {"Origin": ORIGIN, "X-Stamp-Client": "1"}
LINUX = pytest.mark.skipif(os.name != "posix", reason="PROD Linux state and credential ownership")


def approval(key_id, **changes):
    data = dict(format="prod-registration-policy-v1", status="approved", event_id=EVENT,
                recipient_key_id=key_id, notice_version=NOTICE, notice_text="Synthetic fixture notice.",
                purpose="Synthetic purpose", legal_basis_ref="fixture-review-record",
                retention="Fixture retention", inquiry_contact="Fixture help desk",
                approved_at="2001-01-01T09:00:00+09:00")
    data.update(changes)
    return json.dumps(data).encode()


def read_policy(descriptor, path, raw):
    path.write_bytes(raw)
    return prod.contact.ProductionEnrollment.read(
        descriptor, EVENT, path, approved_policy_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("changes", [
    {"format": "synthetic-contact-v1"}, {"status": "draft"}, {"event_id": "demo-foreign"},
    {"recipient_key_id": "a" * 64}, {"notice_version": "synthetic-contact-v1"},
    {"notice_text": ""}, {"purpose": None}, {"legal_basis_ref": " "}, {"retention": ""},
    {"inquiry_contact": False}, {"approved_at": "2001-01-01T00:00:00"}, {"approved_at": "2999-01-01T00:00:00Z"},
])
def test_incomplete_or_wrong_pinned_policy_stays_closed(tmp_path, changes):
    _, descriptor = make_config(tmp_path)
    recipient = prod.contact.ProductionEnrollment.read(descriptor, EVENT)
    path = tmp_path / "notice.json"
    assert read_policy(descriptor, path, approval(recipient.key_id, **changes)).approved_notice() is None


def test_missing_unpinned_changed_or_withdrawn_policy(tmp_path):
    _, descriptor = make_config(tmp_path)
    policy = tmp_path / "notice.json"
    recipient = prod.contact.ProductionEnrollment.read(descriptor, EVENT, policy)
    raw = approval(recipient.key_id)
    assert recipient.approved_notice() is None
    pinned = read_policy(descriptor, policy, raw)
    assert recipient.approved_notice() is None
    assert pinned.approved_notice()["notice_version"] == NOTICE
    policy.write_bytes(raw + b" ")
    assert pinned.approved_notice() is None
    policy.unlink()
    assert pinned.approved_notice() is None


@LINUX
def test_closed_production_routes_catalogue_and_missing_link_storage(tmp_path):
    state, config, descriptor, recipient = ready_state(tmp_path)
    app = prod.prod_app(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    client = app.test_client()
    response = client.get("/", base_url=ORIGIN)
    assert response.status_code == 200
    page = response.text
    assert 'data-registration="closed"' in page
    assert "합성 검수" not in page and "복구 링크" not in page
    assert all(element not in page for element in ("recovery-button", "recovery-dialog", "restore-dialog", "/static/stamps/"))
    assert page.count('type="checkbox"') == 1 and "개인정보 처리 안내를 확인했습니다." in page
    assert page.count("Synthetic description ") == 10
    for path in ("/recover", "/static/stamps/cookie.png", "/static/stamp-art.js", "/static/stamp-art.svg"):
        assert client.get(path, base_url=ORIGIN).status_code == 404
    for path in ("/api/recovery-link", "/api/recover"):
        assert client.post(path, base_url=ORIGIN, headers=HEADERS, json={}).status_code == 404
    assert {"/recover", "/api/recovery-link", "/api/recover"}.isdisjoint(rule.rule for rule in app.url_map.iter_rules())
    assert client.post("/api/register", base_url=ORIGIN, json={}, headers=HEADERS).json["code"] == "registration_closed"
    response = client.post("/api/register", base_url=ORIGIN, json=REGISTER, headers=HEADERS)
    assert response.status_code == 403 and "Set-Cookie" not in response.headers
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery'").fetchone()
        for table in ("customers", "customer_profiles", "customer_contacts", "sessions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(ValueError):
        prod.application.init_db(prod.database_path(state), prod.load_prod_event(config))


@LINUX
def test_pinned_notice_signup_masked_sealed_unique_and_find_after_closure(tmp_path, monkeypatch):
    secret = PrivateKey.generate()
    state, config, descriptor, recipient = ready_state(tmp_path, secret.public_key)
    policy = tmp_path / "notice.json"
    recipient = read_policy(descriptor, policy, approval(recipient.key_id))
    app = prod.prod_app(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    during_collection = datetime.fromisoformat("2026-12-12T12:00:00+09:00").timestamp()
    monkeypatch.setattr(prod.application.time, "time", lambda: during_collection)
    client = app.test_client()
    page = client.get("/", base_url=ORIGIN).text
    assert 'data-registration="production"' in page and "Synthetic fixture notice." in page
    assert "fixture-review-record" not in page and "합성 검수" not in page

    def post(browser, path, payload):
        return browser.post(path, base_url=ORIGIN, headers=HEADERS, json=payload)
    assert post(client, "/api/register", {**REGISTER, "notice_acknowledged": False}).status_code == 400
    assert post(client, "/api/register", {**REGISTER, "notice_version": prod.contact.NOTICE}).json["code"] == "notice_changed"
    registered = post(client, "/api/register", REGISTER)
    assert registered.status_code == 200
    cookie = registered.headers["Set-Cookie"]
    assert cookie.startswith("__Host-maison-prod-collection=") and "Secure" in cookie and "HttpOnly" in cookie
    current = client.get("/api/state", base_url=ORIGIN)
    assert current.json["customer"]["display_name"] == NAME and "recovery_saved" not in current.json["customer"]
    assert NUMBER not in current.text and "phone" not in current.json["customer"]
    second = app.test_client()
    assert post(second, "/api/register", {**REGISTER, "name": "Other Fixture"}).json["code"] == "phone_registered"
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        profile = db.execute("SELECT * FROM customer_profiles").fetchone()
        contact = db.execute("SELECT * FROM customer_contacts").fetchone()
        key = prod.application.read_profile_key(prod.application.default_profile_key_path(prod.database_path(state)))
        assert profile["phone_lookup_hmac"] == hmac.digest(key, NUMBER.encode(), "sha256").hex()
        assert profile["notice_version"] == NOTICE and contact["key_id"] == recipient.key_id
        assert contact["phone_masked"] == prod.contact.masked(NUMBER)
        assert json.loads(SealedBox(secret).decrypt(contact["sealed_phone"])) == [
            prod.contact.PROD_CONTACT, EVENT, profile["customer_id"], NUMBER]
        assert NUMBER not in repr(tuple(profile)) + repr(tuple(contact))
    policy.unlink()
    assert post(second, "/api/register", REGISTER).json["code"] == "registration_closed"
    assert post(second, "/api/find", {**REGISTER, "name": "Other Fixture"}).json["code"] == "profile_not_found"
    assert post(second, "/api/find", {"name": NAME, "phone": "+82 " + NUMBER[1:]}).status_code == 200
    assert second.get("/api/state", base_url=ORIGIN).json["customer"]["display_name"] == NAME
    staff = app.test_client()
    password = secrets.token_urlsafe(24)
    prod.staff_admin.write_staff_password(prod.database_path(state), "fixture-counter", password, production=True)
    csrf = post(staff, "/api/admin/login", {"password": password}).json["csrf"]
    found = staff.post("/api/admin/search", base_url=ORIGIN, headers={**HEADERS, "X-Admin-CSRF": csrf},
                       json={"phone": NUMBER})
    assert found.status_code == 200 and found.json["phone_masked"] == prod.contact.masked(NUMBER)
    assert NUMBER not in found.text and "sealed_phone" not in found.json and "phone_lookup_hmac" not in found.json
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


@LINUX
def test_policy_withdrawn_while_waiting_for_write_lock(tmp_path, monkeypatch):
    state, config, descriptor, recipient = ready_state(tmp_path)
    policy = tmp_path / "notice.json"
    recipient = read_policy(descriptor, policy, approval(recipient.key_id))
    app = prod.prod_app(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    monkeypatch.setattr(prod.application.time, "time", lambda: 1800000000)
    before_lock = threading.Event()
    original = type(recipient).approved_notice
    def notify(self):
        result = original(self)
        before_lock.set()
        return result
    monkeypatch.setattr(type(recipient), "approved_notice", notify)
    responses = []
    def register():
        responses.append(app.test_client().post("/api/register", base_url=ORIGIN, headers=HEADERS, json=REGISTER))
    with app.write_lock:
        worker = threading.Thread(target=register)
        worker.start()
        assert before_lock.wait(5)
        policy.unlink()
    worker.join(timeout=10)
    assert len(responses) == 1 and responses[0].status_code == 403
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
