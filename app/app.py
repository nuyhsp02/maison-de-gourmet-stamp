"""One event, one laptop, anonymous collections. No GET writes customer data."""
import hashlib
from contextlib import closing
import json
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parent
LIFETIME = 90 * 86400
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("날짜를 시간대가 있는 ISO 8601 문자열로 지정하세요.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("날짜에 +09:00 등 시간대가 필요합니다.")
    return int(parsed.timestamp())


def load_event(path):
    event = json.loads(Path(path).read_text(encoding="utf-8"))
    products = event.get("products", [])
    ids = [p["id"] for p in products]
    if len(ids) != 10 or len(set(ids)) != 10:
        raise ValueError("서로 다른 상품 ID 10개가 필요합니다.")
    if sum(p["category"] == "season" for p in products) != 4 or sum(
        p["category"] == "select" for p in products
    ) != 6:
        raise ValueError("시즌 4개와 지정 상품 6개가 필요합니다.")
    for p in products:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", p["id"]) or not p.get("name"):
            raise ValueError("상품 ID 또는 이름이 잘못되었습니다.")
    if not isinstance(event.get("draft"), bool) or not event.get("id"):
        raise ValueError("이벤트 id와 draft(boolean)가 필요합니다.")
    if not event["draft"]:
        start, end, reward = [timestamp(event.get(k)) for k in (
            "starts_at", "ends_at", "reward_until")]
        if not start < end <= reward or end - start > 32 * 86400:
            raise ValueError("이벤트 기간은 32일 이내, 보상 마감은 종료 이후여야 합니다.")
    return event


def connect_db(path):
    db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def init_db(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite connection context managers commit/rollback but do not close the handle.
    # Close explicitly so Windows can move a stopped database during recovery.
    with closing(connect_db(path)) as db:
        db.executescript((ROOT / "schema.sql").read_text())
        signature = json.dumps([event["id"], sorted(p["id"] for p in event["products"])])
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = db.execute("SELECT value FROM metadata WHERE key='event'").fetchone()
            if existing and existing[0] != signature:
                raise ValueError("기존 DB의 이벤트/상품 ID가 다릅니다. 초기화하지 말고 설정을 확인하세요.")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('event', ?)", (signature,))
            for p in event["products"]:
                db.execute("INSERT OR IGNORE INTO products VALUES (?, ?)",
                           (p["id"], secrets.token_urlsafe(24)))
            db.commit()
        except Exception:
            db.rollback()
            raise
    path.chmod(0o600)


class Limits:
    """Bounded, thread-safe limiter. One waitress process; no IP logging."""
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    def allow(self, key, maximum, window=60):
        now = time.monotonic()
        with self.lock:
            queue = self.entries.pop(key, deque())
            while queue and queue[0] < now - window:
                queue.popleft()
            allowed = len(queue) < maximum
            if allowed:
                queue.append(now)
            self.entries[key] = queue
            while len(self.entries) > 4096:
                self.entries.popitem(last=False)
            return allowed


class APIError(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        self.message, self.status, self.code = message, status, code


def create_app(event_path=None, database=None, public_url="http://127.0.0.1:8765",
               dev=False, testing=False):
    event = load_event(event_path or ROOT / "event.json")
    parsed = urlsplit(public_url)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("PUBLIC_URL에는 경로/인증정보 없이 도메인과 포트만 지정하세요.")
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("올바른 공개 URL이 필요합니다.")
    if dev:
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("개발 모드는 이 노트북의 localhost HTTP에서만 허용됩니다.")
    elif parsed.scheme != "https" or event["draft"]:
        raise ValueError("실운영에는 HTTPS URL과 draft=false인 확정 이벤트 설정이 필요합니다.")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    app = Flask(__name__)
    app.config.update(TESTING=testing, MAX_CONTENT_LENGTH=4096,
                      DATABASE=str(database or ROOT / "data" / "collection.sqlite"))
    app.event = event
    app.public_url = origin
    app.cookie_name = "collection_dev" if dev else "__Host-collection"
    app.limits = Limits()
    # One Waitress process, one SQLite writer. Coordinate request writers here
    # instead of letting Windows busy retries repeatedly race for the DB lock.
    app.write_lock = threading.Lock()
    init_db(app.config["DATABASE"], event)

    def db():
        if "db" not in g:
            g.db = connect_db(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        try:
            if connection is not None:
                connection.close()
        finally:
            if g.pop("owns_write_lock", False):
                app.write_lock.release()

    def phase():
        now = int(time.time())
        if event["draft"]:
            return "demo"
        if now < timestamp(event["starts_at"]):
            return "upcoming"
        if now >= timestamp(event["ends_at"]):
            return "ended"
        return "running"

    def reward_open():
        return event["draft"] or timestamp(event["starts_at"]) <= time.time() < timestamp(event["reward_until"])

    def require_active():
        if phase() not in ("demo", "running"):
            raise APIError("지금은 스탬프 적립 기간이 아닙니다.", 409, "event_closed")

    def identity():
        cookie = request.cookies.get(app.cookie_name, "")
        if not TOKEN_RE.fullmatch(cookie):
            return None
        return db().execute(
            "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
            (digest(cookie), int(time.time()))).fetchone()

    def require_identity():
        who = identity()
        if who is None:
            raise APIError("브라우저 기록을 확인할 수 없습니다. 쿠키를 허용하거나 복구 링크를 사용하세요.",
                           401, "session_required")
        if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), who["csrf"]):
            raise APIError("화면을 새로고침한 후 다시 시도하세요.", 403, "csrf_failed")
        return who

    def body():
        result = request.get_json(silent=True)
        if not isinstance(result, dict):
            raise APIError("올바른 요청 형식이 아닙니다.")
        return result

    def limited(key, maximum):
        if not testing and not app.limits.allow(key, maximum):
            raise APIError("요청이 많습니다. 1분 뒤 다시 시도하세요.", 429, "rate_limited")

    @app.before_request
    def check_request():
        # Validate configured host even in development (DNS rebinding defense).
        if request.host.lower() != parsed.netloc.lower():
            raise APIError("설정된 주소로 접속해 주세요.", 400, "invalid_host")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("Origin") != origin or request.headers.get("X-Stamp-Client") != "1":
                raise APIError("사이트 내 버튼으로 다시 시도하세요.", 403, "invalid_origin")
            if request.mimetype != "application/json":
                raise APIError("JSON 요청만 허용됩니다.", 415)
            # 30 visitors behind one café NAT must remain usable.
            limited(("writes", request.remote_addr), 1800)
            if not app.write_lock.acquire(timeout=15):
                raise APIError("잠시 후 다시 시도하세요. 서버가 기록을 처리하고 있습니다.",
                               503, "storage_unavailable")
            g.owns_write_lock = True

    @app.after_request
    def security_headers(response):
        response.headers.update({
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        if not dev:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if response.status_code == 429:
            response.headers["Retry-After"] = "60"
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=error.message, code=error.code), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="요청을 처리할 수 없습니다.", code=f"http_{error.code}"), error.code

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        app.logger.error("Database operation failed: %s (%s)", type(error).__name__,
                         getattr(error, "sqlite_errorname", "unknown"))
        return jsonify(error="잠시 후 다시 시도하세요. 서버가 기록을 처리하고 있습니다.",
                       code="storage_unavailable"), 503

    def snapshot(who=None, qr=None):
        # Products never disclose the fixed QR URLs to the stamp board.
        public_event = {k: v for k, v in event.items() if k != "id"}
        result = {"event": public_event, "phase": phase(), "reward_open": reward_open(),
                  "customer": None, "csrf": None, "pending": None}
        if who is not None:
            customer = db().execute("SELECT * FROM customers WHERE id=?", (who["customer_id"],)).fetchone()
            stamps = db().execute("SELECT product_id, collected_at FROM stamps WHERE customer_id=?",
                                  (who["customer_id"],)).fetchall()
            recovery = db().execute("SELECT expires_at FROM recovery WHERE customer_id=? AND expires_at>?",
                                    (who["customer_id"], int(time.time()))).fetchone()
            result.update(csrf=who["csrf"], customer={
                "stamps": {s["product_id"]: s["collected_at"] for s in stamps},
                "count": len(stamps), "status": "redeemed" if customer["redeemed_at"] else
                    ("complete" if len(stamps) == 10 else "collecting"),
                "redeemed_at": customer["redeemed_at"],
                "recovery_saved": bool(recovery),
            })
        if qr:
            product = db().execute("SELECT id FROM products WHERE qr_key=?", (qr,)).fetchone()
            if not product:
                raise APIError("이 이벤트의 QR이 아닙니다. 매장의 QR을 다시 스캔해 주세요.", 404, "invalid_qr")
            result["pending"] = product["id"]
        return result

    def new_session(customer_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db().execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                     (digest(token), customer_id, csrf, int(time.time()) + LIFETIME))
        return token

    def set_cookie(response, token):
        response.set_cookie(app.cookie_name, token, max_age=LIFETIME, secure=not dev,
                            httponly=True, samesite="Lax", path="/")
        return response

    @app.get("/")
    @app.get("/recover")
    @app.get("/q/<qr_key>")
    def page(qr_key=None):
        if qr_key and not db().execute("SELECT 1 FROM products WHERE qr_key=?", (qr_key,)).fetchone():
            raise APIError("사용할 수 없는 QR 주소입니다.", 404)
        # Read-only eligibility hint, not an authorization token. Missing browser
        # navigation signals fail closed to a manual button. GET never collects.
        purpose = " ".join(request.headers.get(name, "") for name in
                           ("Purpose", "Sec-Purpose", "X-Purpose")).lower()
        auto_collect = bool(qr_key and request.method == "GET" and not purpose.strip()
                            and request.headers.get("Sec-Fetch-User") == "?1"
                            and request.headers.get("Sec-Fetch-Mode") == "navigate"
                            and request.headers.get("Sec-Fetch-Dest") == "document")
        return render_template("index.html", auto_collect=auto_collect)

    @app.get("/health")
    def health():
        db().execute("SELECT 1").fetchone()
        return jsonify(ok=True)

    @app.get("/api/state")
    def state():
        return jsonify(snapshot(identity(), request.args.get("qr")))

    @app.post("/api/session")
    def start():
        body()
        who = identity()
        if who:
            require_identity()
            return jsonify(snapshot(who))
        require_active()
        limited(("new", request.remote_addr), 120)
        customer_id = secrets.token_hex(16)
        db().execute("BEGIN IMMEDIATE")
        try:
            db().execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
            db().execute("INSERT INTO customers VALUES (?, ?, NULL)", (customer_id, int(time.time())))
            token = new_session(customer_id)
            db().commit()
        except Exception:
            db().rollback()
            raise
        # Client reads state again to prove the browser accepted its cookie.
        return set_cookie(jsonify(ok=True), token)

    @app.post("/api/collect")
    def collect():
        who = require_identity()
        limited(("collect", who["customer_id"]), 90)
        require_active()
        qr = body().get("qr")
        if not isinstance(qr, str) or len(qr) > 100:
            raise APIError("QR을 다시 스캔해 주세요.")
        product = db().execute("SELECT id FROM products WHERE qr_key=?", (qr,)).fetchone()
        if not product:
            raise APIError("이 이벤트의 QR이 아닙니다.", 404, "invalid_qr")
        db().execute("BEGIN IMMEDIATE")
        try:
            redeemed = db().execute("SELECT redeemed_at FROM customers WHERE id=?", (who["customer_id"],)).fetchone()[0]
            added = False
            if not redeemed:
                cursor = db().execute("INSERT OR IGNORE INTO stamps VALUES (?, ?, ?)",
                                      (who["customer_id"], product["id"], int(time.time())))
                added = cursor.rowcount == 1
            db().commit()
        except Exception:
            db().rollback()
            raise
        return jsonify(**snapshot(who), added=added, product_id=product["id"])

    @app.post("/api/recovery-link")
    def recovery_link():
        who = require_identity()
        if body().get("confirm_rotation") is not True:
            raise APIError("기존 링크가 무효가 되는 것을 확인해 주세요.")
        limited(("issue_recovery", who["customer_id"]), 10)
        token = secrets.token_urlsafe(32)
        expiry = int(time.time()) + LIFETIME
        db().execute("INSERT INTO recovery VALUES (?, ?, ?) ON CONFLICT(customer_id) DO UPDATE "
                     "SET token_hash=excluded.token_hash, expires_at=excluded.expires_at",
                     (who["customer_id"], digest(token), expiry))
        return jsonify(url=f"{origin}/recover#{token}", expires_at=expiry)

    @app.post("/api/recover")
    def recover():
        limited(("recover", request.remote_addr), 60)
        data = body()
        token = data.get("token", "")
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise APIError("복구 링크가 잘못되었거나 만료되었습니다.", 400, "invalid_recovery")
        db().execute("BEGIN IMMEDIATE")
        try:
            target = db().execute("SELECT * FROM recovery WHERE token_hash=? AND expires_at>?",
                                  (digest(token), int(time.time()))).fetchone()
            if not target:
                raise APIError("복구 링크가 잘못되었거나 만료되었습니다.", 400, "invalid_recovery")
            current = identity()
            if current and current["customer_id"] != target["customer_id"] and data.get("confirm_switch") is not True:
                raise APIError("현재 브라우저의 다른 스탬프판을 복구한 기록으로 바꾸게 됩니다. 기록은 합쳐지지 않습니다.",
                               409, "confirm_switch")
            if current:
                require_identity()
                db().execute("DELETE FROM sessions WHERE token_hash=?", (current["token_hash"],))
            cookie = new_session(target["customer_id"])
            db().commit()
        except Exception:
            db().rollback()
            raise
        return set_cookie(jsonify(ok=True), cookie)

    @app.post("/api/redeem")
    def redeem():
        who = require_identity()
        if body().get("confirm_received") is not True:
            raise APIError("실물 보상을 지급한 뒤 확인해 주세요.")
        limited(("redeem", who["customer_id"]), 20)
        db().execute("BEGIN IMMEDIATE")
        try:
            customer_id = who["customer_id"]
            row = db().execute("SELECT redeemed_at FROM customers WHERE id=?", (customer_id,)).fetchone()
            if row[0] is not None:
                db().commit()
                return jsonify(**snapshot(who), already_redeemed=True)
            if not reward_open():
                raise APIError("보상 지급 기간이 아닙니다. 직원에게 문의해 주세요.", 409, "reward_closed")
            count = db().execute("SELECT COUNT(*) FROM stamps WHERE customer_id=?", (customer_id,)).fetchone()[0]
            if count != 10:
                raise APIError("10개 상품을 모두 모아야 보상을 받을 수 있습니다.", 409, "incomplete")
            now = int(time.time())
            db().execute("UPDATE customers SET redeemed_at=? WHERE id=? AND redeemed_at IS NULL", (now, customer_id))
            db().execute("INSERT INTO reward_audit VALUES (?, ?, 'unauthenticated_customer_screen')", (customer_id, now))
            db().commit()
        except Exception:
            db().rollback()
            raise
        return jsonify(**snapshot(who), already_redeemed=False)

    return app
