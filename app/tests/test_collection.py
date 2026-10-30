import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app import ROOT, Limits, connect_db, create_app, digest
from conftest import BASE, Visitor


def test_get_head_prefetch_never_create_customer_or_stamp(app, keys):
    client = app.test_client()
    for path in ["/", "/q/" + keys[0], "/api/state?qr=" + keys[0], "/recover"]:
        for method in ["GET", "HEAD"]:
            response = client.open(path, method=method, base_url=BASE,
                                   headers={"Purpose": "prefetch"})
            assert response.status_code == 200
            assert "Set-Cookie" not in response.headers
    with connect_db(app.config["DATABASE"]) as db:
        for table in ["customers", "sessions", "stamps", "recovery", "reward_audit"]:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_explicit_start_cookie_session_and_unique_products(app, keys):
    visitor = Visitor(app)
    cookie = visitor.client.get_cookie(app.cookie_name, domain=visitor.domain)
    assert cookie.http_only and cookie.same_site == "Lax" and cookie.path == "/"
    assert cookie.max_age == 90 * 86400
    assert visitor.read()["customer"]["count"] == 0
    assert visitor.collect(keys[-1])["added"] is True
    assert visitor.collect(keys[-1])["added"] is False
    assert visitor.read()["customer"]["count"] == 1
    assert Visitor(app, cookie=visitor.cookie()).state["customer"]["count"] == 1
    assert Visitor(app, start=False).state["customer"] is None


def test_server_enforces_origin_json_csrf_and_unknown_qr(app, keys):
    v = Visitor(app)
    c = v.client
    assert c.post("/api/collect", json={"qr": keys[0]}, base_url=BASE).status_code == 403
    assert c.post("/api/collect", json={"qr": keys[0]}, base_url=BASE, headers={
        "Origin": "https://attacker.invalid", "X-Stamp-Client": "1", "X-CSRF-Token": v.csrf}).status_code == 403
    assert c.post("/api/collect", json={"qr": keys[0]}, base_url=BASE, headers={
        "Origin": BASE, "X-Stamp-Client": "1"}).status_code == 403
    assert c.post("/api/session", data="x", base_url=BASE, headers={
        "Origin": BASE, "X-Stamp-Client": "1"}).status_code == 415
    assert v.post("/api/collect", {"qr": "season-01"}).status_code == 404
    assert v.post("/api/collect", {"qr": None}).status_code == 400
    assert c.get("/", base_url="http://evil.invalid").status_code == 400
    assert c.get("/q/not-valid", base_url=BASE).status_code == 404
    assert v.read()["customer"]["count"] == 0
    assert v.post("/api/collect", {"qr": "x" * 5000}).status_code == 413
    assert "qr_key" not in json.dumps(v.read())
    assert keys[0] not in json.dumps(v.read())


def test_completion_redemption_confirmation_and_permanent_collection(app, keys):
    v = Visitor(app)
    assert v.post("/api/redeem", {"confirm_received": True}).status_code == 409
    for key in reversed(keys):
        v.collect(key)
    assert v.read()["customer"]["status"] == "complete"
    assert v.post("/api/redeem", {"confirm_received": False}).status_code == 400
    response = v.post("/api/redeem", {"confirm_received": True})
    assert response.json["already_redeemed"] is False
    assert response.json["customer"]["status"] == "redeemed"
    assert v.post("/api/redeem", {"confirm_received": True}).json["already_redeemed"] is True
    assert v.collect(keys[0])["added"] is False
    assert v.read()["customer"]["count"] == 10
    assert v.client.post("/api/reset", json={}, base_url=BASE, headers={
        "Origin": BASE, "X-Stamp-Client": "1"}).status_code == 404
    restarted = create_app(database=app.config["DATABASE"], public_url=BASE, dev=True, testing=True)
    assert Visitor(restarted, cookie=v.cookie()).state["customer"]["status"] == "redeemed"
    with connect_db(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM reward_audit").fetchone()[0] == 1
        assert [r[0] for r in db.execute("SELECT qr_key FROM products ORDER BY id")] == keys


def test_parallel_duplicate_collect_and_redeem(app, keys):
    v = Visitor(app)
    cookie = v.cookie()
    def collect(_):
        return Visitor(app, cookie=cookie).collect(keys[0])["added"]
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert sum(pool.map(collect, range(30))) == 1
    for key in keys[1:]:
        v.collect(key)
    def redeem(_):
        response = Visitor(app, cookie=cookie).post("/api/redeem", {"confirm_received": True})
        assert response.status_code == 200
        return not response.json["already_redeemed"]
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert sum(pool.map(redeem, range(30))) == 1
    assert v.read()["customer"]["count"] == 10


def test_recovery_hashes_rotation_conflict_and_preserves_redeemed(app, keys):
    v = Visitor(app)
    for key in keys: v.collect(key)
    v.post("/api/redeem", {"confirm_received": True})
    assert v.post("/api/recovery-link", {}).status_code == 400
    old = v.post("/api/recovery-link", {"confirm_rotation": True}).json["url"].split("#")[1]
    token = v.post("/api/recovery-link", {"confirm_rotation": True}).json["url"].split("#")[1]
    assert len(token) == 43 and old != token
    other = Visitor(app)
    assert other.post("/api/recover", {"token": old}).status_code == 400
    assert other.post("/api/recover", {"token": token}).json["code"] == "confirm_switch"
    old_cookie = other.cookie()
    assert other.post("/api/recover", {"token": token, "confirm_switch": True}).status_code == 200
    assert other.read()["customer"]["status"] == "redeemed"
    assert other.read()["customer"]["count"] == 10
    assert other.cookie() != old_cookie
    fresh = Visitor(app, start=False)
    assert fresh.post("/api/recover", {"token": token}).status_code == 200
    assert fresh.read()["customer"]["status"] == "redeemed"
    with connect_db(app.config["DATABASE"]) as db:
        assert db.execute("SELECT token_hash FROM recovery").fetchone()[0] == digest(token)
        assert not db.execute("SELECT 1 FROM sessions WHERE token_hash=?", (digest(old_cookie),)).fetchone()
        dump = '\n'.join(db.iterdump())
        assert token not in dump and v.cookie() not in dump
        db.execute("UPDATE recovery SET expires_at=0")
    assert fresh.post("/api/recover", {"token": token}).status_code == 400
    assert fresh.post("/api/recover", {"token": "x"}).status_code == 400


def test_expired_session_cannot_collect(app, keys):
    v = Visitor(app)
    with connect_db(app.config["DATABASE"]) as db:
        db.execute("UPDATE sessions SET expires_at=0")
    assert v.post("/api/collect", {"qr": keys[0]}).status_code == 401
    assert v.read()["customer"] is None


def real_config(tmp_path, start, end, reward):
    event = json.loads((ROOT / "event.json").read_text())
    event.update(draft=False, starts_at=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                 ends_at=datetime.fromtimestamp(end, timezone.utc).isoformat(),
                 reward_until=datetime.fromtimestamp(reward, timezone.utc).isoformat())
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return path


def test_production_secure_cookie_headers_and_period_boundaries(tmp_path, monkeypatch):
    clock = 1800000000
    config = real_config(tmp_path, clock, clock + 30 * 86400, clock + 31 * 86400)
    app = create_app(config, tmp_path / "prod.sqlite", "https://cafe.example", testing=True)
    monkeypatch.setattr("app.time.time", lambda: clock - 1)
    v = Visitor(app, start=False, base="https://cafe.example")
    assert v.read()["phase"] == "upcoming"
    assert v.post("/api/session", {}).status_code == 409
    monkeypatch.setattr("app.time.time", lambda: clock)
    response = v.post("/api/session", {})
    cookie = response.headers["Set-Cookie"]
    assert "__Host-collection=" in cookie and "Secure" in cookie and "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie and "Domain=" not in cookie
    assert "max-age=" in response.headers["Strict-Transport-Security"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    v.read()
    with connect_db(app.config["DATABASE"]) as db:
        keys = [r[0] for r in db.execute("SELECT qr_key FROM products")]
    for key in keys: v.collect(key)
    monkeypatch.setattr("app.time.time", lambda: clock + 30 * 86400)
    assert v.read()["phase"] == "ended"
    assert v.post("/api/collect", {"qr": keys[0]}).status_code == 409
    assert v.post("/api/redeem", {"confirm_received": True}).status_code == 200
    monkeypatch.setattr("app.time.time", lambda: clock + 31 * 86400)
    assert v.read()["reward_open"] is False
    assert v.post("/api/redeem", {"confirm_received": True}).json["already_redeemed"] is True


def test_reward_after_deadline_rejected(tmp_path, monkeypatch):
    clock = 1800000000
    config = real_config(tmp_path, clock, clock + 100, clock + 200)
    monkeypatch.setattr("app.time.time", lambda: clock)
    app = create_app(config, tmp_path / "prod.sqlite", "https://cafe.example", testing=True)
    v = Visitor(app, base="https://cafe.example")
    with connect_db(app.config["DATABASE"]) as db:
        keys = [r[0] for r in db.execute("SELECT qr_key FROM products")]
    for key in keys: v.collect(key)
    monkeypatch.setattr("app.time.time", lambda: clock + 200)
    assert v.post("/api/redeem", {"confirm_received": True}).json["code"] == "reward_closed"


def test_reject_unsafe_startup_and_event_id_change(app, tmp_path):
    for url, dev in [("http://cafe.example", False), ("https://cafe.example", False),
                     ("http://192.168.0.2:8765", True), (BASE + "/subpath", True)]:
        with pytest.raises(ValueError): create_app(public_url=url, dev=dev, database=tmp_path / "new.sqlite")
    event = json.loads((ROOT / "event.json").read_text())
    event["id"] = "different-event"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(event))
    with pytest.raises(ValueError):
        create_app(path, app.config["DATABASE"], BASE, dev=True)


def test_thread_safe_bounded_rate_limiter():
    limiter = Limits()
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert sum(pool.map(lambda _: limiter.allow("same", 10), range(30))) == 10
    for key in range(5000): limiter.allow(key, 1)
    assert len(limiter.entries) <= 4096


def test_rate_limits_active_in_non_test_app(tmp_path):
    app = create_app(database=tmp_path / "limited.sqlite", public_url=BASE, dev=True)
    v = Visitor(app)
    for _ in range(10): assert v.post("/api/recovery-link", {"confirm_rotation": True}).status_code == 200
    response = v.post("/api/recovery-link", {"confirm_rotation": True})
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"
