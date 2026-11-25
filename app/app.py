"""Single-event stamp cards. No GET writes customer data."""
import hashlib
import hmac
from contextlib import closing, contextmanager
import json
import os
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

if __package__:
    from . import contact_privacy as contact
    from .staff_admin import install_admin, retire_restored_manager, validate_admin_schema
else:
    import contact_privacy as contact
    from staff_admin import install_admin, retire_restored_manager, validate_admin_schema

ROOT = Path(__file__).resolve().parent
LIFETIME = 90 * 86400
MAX_EVENT_DAYS = 62  # Two consecutive calendar months can span 62 days.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NOTICE_VERSION = contact.NOTICE
DEMO_CATALOG_EVENT = "demo-gourmetstamp-20261125-rehearsal"


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
        if not start < end <= reward or end - start > MAX_EVENT_DAYS * 86400:
            raise ValueError(f"이벤트 기간은 {MAX_EVENT_DAYS}일 이내이며, 보상 마감은 종료 시각과 같거나 늦어야 합니다.")
    if event["id"] == DEMO_CATALOG_EVENT:
        catalog = json.loads((ROOT / "demo-product-catalog.json").read_text(encoding="utf-8"))
        if catalog["event_id"] != event["id"] or catalog["products"].keys() != set(ids):
            raise ValueError("시연 행사와 상품 목록의 ID를 확인해 주세요.")
        for product in products:
            labels = catalog["products"][product["id"]]
            product["name"] = labels["name"]
            product["short_name"] = labels["short_name"]
            product["stamp_image"] = labels["stamp_image"]
    return event


def connect_db(path, *, mode=None):
    if mode is None:
        db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    else:
        if mode not in ("ro", "rw"):
            raise ValueError("기존 DB의 읽기 또는 읽기/쓰기 모드를 지정하세요.")
        uri = Path(path).absolute().as_uri() + "?mode=" + mode
        db = sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 15000")
    except BaseException:
        db.close()
        raise
    return db


def default_profile_key_path(database):
    return Path(str(database) + ".profile.key")


def key_fingerprint(key):
    return hmac.digest(key, b"maison/profile-key-check/v1", "sha256").hex()


def read_profile_key(path):
    try:
        key = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError("전화번호 검색 키를 읽을 수 없습니다. 보관한 원본 키를 복원하세요.") from exc
    if len(key) != 32:
        raise ValueError("전화번호 검색 키 형식이 잘못되었습니다. 보관한 원본 키를 복원하세요.")
    return key


def verify_profile_key(db, key_path):
    """Read-only check for startup and staged restores, including pre-profile DBs."""
    marker = db.execute("SELECT value FROM metadata WHERE key='profile_key_check_v1'").fetchone()
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_profiles'").fetchone()
    if marker is None:
        if has_table and db.execute("SELECT 1 FROM customer_profiles LIMIT 1").fetchone():
            raise ValueError("등록 정보의 키 확인값이 없습니다. DB와 키의 백업을 확인하세요.")
        return None
    if not has_table:
        raise ValueError("키 확인값에 대응하는 고객 등록 테이블이 없습니다. 백업을 확인하세요.")
    key = read_profile_key(key_path)
    if not hmac.compare_digest(key_fingerprint(key), marker[0]):
        raise ValueError("전화번호 검색 키가 이 DB와 일치하지 않습니다. 원본 키를 복원하세요.")
    return key


def bind_profile_key(db, path):
    # Serialized by the migration transaction. Never replace an existing secret.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        key = read_profile_key(path)
    else:
        with os.fdopen(fd, "wb") as stream:
            key = secrets.token_bytes(32)
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    db.execute("INSERT INTO metadata VALUES ('profile_key_check_v1', ?)", (key_fingerprint(key),))
    return key


def apply_schema(db):
    # executescript() implicitly commits an open transaction on supported runtimes.
    # Keep validation, additive DDL and key binding in the same transaction.
    statement = ""
    for line in (ROOT / "schema.sql").read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("schema.sql에 완료되지 않은 SQL 문이 있습니다.")


def event_signature(event):
    return json.dumps([event["id"], sorted(p["id"] for p in event["products"])])


def init_db(path, event, profile_key_path=None):
    path = Path(path)
    key_path = profile_key_path or default_profile_key_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite connection context managers commit/rollback but do not close the handle.
    # Close explicitly so Windows can move a stopped database during recovery.
    with closing(connect_db(path)) as db:
        signature = event_signature(event)
        try:
            db.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            key = existing = None
            if tables:
                if "metadata" not in tables:
                    raise ValueError("기존 DB에 행사 메타데이터가 없습니다. 백업을 확인하세요.")
                existing = db.execute("SELECT value FROM metadata WHERE key='event'").fetchone()
                if existing and existing[0] != signature:
                    raise ValueError("기존 DB의 이벤트/상품 ID가 다릅니다. 초기화하지 말고 설정을 확인하세요.")
                if existing:
                    stored_ids = {row[0] for row in db.execute("SELECT id FROM products")}
                    if stored_ids != {p["id"] for p in event["products"]}:
                        raise ValueError("기존 DB에 등록된 상품 구성이 다릅니다. 원래 QR이 보존된 백업을 확인하세요.")
                key = verify_profile_key(db, key_path)
                validate_admin_schema(db)
            apply_schema(db)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('event', ?)", (signature,))
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('admin_schema_v1', '1')")
            if key is None:
                key = bind_profile_key(db, key_path)
            if existing is None:
                for p in event["products"]:
                    db.execute("INSERT OR IGNORE INTO products VALUES (?, ?)",
                               (p["id"], secrets.token_urlsafe(24)))
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.execute("PRAGMA journal_mode = WAL")
    path.chmod(0o600)
    return key


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


def normalized_phone(value):
    digits = contact.phone_digits(value)
    if digits is not None:
        return digits
    raise APIError("국내 휴대전화번호를 확인해 주세요.")


def create_app(event_path=None, database=None, public_url="http://127.0.0.1:8765",
               dev=False, testing=False, profile_key_path=None, *, validate_existing=None, synthetic_config=None):
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
    enrollment = None
    if synthetic_config is not None:
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1") and not parsed.hostname.endswith(".invalid"):
            raise ValueError("합성 가입은 로컬 시험 주소에서만 사용할 수 있습니다.")
        enrollment = contact.SyntheticEnrollment.read(synthetic_config, event["id"])
        # Synthetic enrollment requires an explicitly migrated, existing database.
        with closing(connect_db(app.config["DATABASE"], mode="ro")) as source:
            if contact.contact_key_id(source) != enrollment.key_id:
                raise ValueError("공개키와 명시적으로 이관한 DB가 일치해야 합니다.")
    if validate_existing is None:
        profile_key = init_db(app.config["DATABASE"], event, profile_key_path)
    else:
        profile_key = validate_existing(Path(app.config["DATABASE"]), event)
    if enrollment is None:
        with closing(connect_db(app.config["DATABASE"], mode="ro")) as source:
            if contact.contact_key_id(source) is not None:
                raise ValueError("연락처 DB의 공개키 설정이 필요합니다.")
    app.contact_enabled = enrollment is not None

    def db():
        if "db" not in g:
            if validate_existing is None:
                g.db = connect_db(app.config["DATABASE"])
            else:
                g.db = connect_db(app.config["DATABASE"], mode="rw")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @contextmanager
    def write_transaction():
        # Open/configure before admission; request validation also happens outside.
        connection = db()
        if not app.write_lock.acquire(timeout=15):
            app.logger.warning("Write admission timed out")
            raise APIError("잠시 후 다시 시도하세요. 서버가 기록을 처리하고 있습니다.",
                           503, "storage_unavailable")
        try:
            if getattr(app, "storage_failed", False):
                raise APIError("잠시 후 다시 시도하세요. 서버가 기록을 처리하고 있습니다.",
                               503, "storage_unavailable")
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                # Includes cancellation. Do not admit another writer while a failed
                # commit still owns a SQLite transaction.
                try:
                    connection.rollback()
                except BaseException:
                    g.pop("db", None)
                    try:
                        connection.close()
                    except BaseException:
                        app.storage_failed = True
                        app.logger.critical("Database cleanup failed; writes disabled until restart")
                raise
        finally:
            app.write_lock.release()

    def phase(now=None):
        if now is None:
            now = int(time.time())
        if event["draft"]:
            return "demo"
        if now < timestamp(event["starts_at"]):
            return "upcoming"
        if now >= timestamp(event["ends_at"]):
            return "ended"
        return "running"

    def reward_open(now=None):
        if now is None:
            now = time.time()
        return event["draft"] or timestamp(event["starts_at"]) <= now < timestamp(event["reward_until"])

    def staff_window(now):
        if event["draft"]:
            return "collection", None
        if phase(now) != "ended":
            return "collection", timestamp(event["ends_at"])
        if reward_open(now):
            return "correction", timestamp(event["reward_until"])
        return "closed", None

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
            raise APIError("쿠키를 허용하고 기존 스탬프 찾기 또는 저장한 복구 링크를 이용해 주세요.",
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
        if validate_existing is not None and not request.is_secure:
            raise APIError("설정된 HTTPS 주소로 접속해 주세요.", 400, "https_required")
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
    @app.errorhandler(sqlite3.IntegrityError)
    def database_error(error):
        app.logger.error("Database operation failed: %s (%s)", type(error).__name__,
                         getattr(error, "sqlite_errorname", "unknown"))
        return jsonify(error="잠시 후 다시 시도하세요. 서버가 기록을 처리하고 있습니다.",
                       code="storage_unavailable"), 503

    def snapshot(who=None, qr=None):
        # One read snapshot for the customer/status/stamp queries. This may reflect
        # a later committed write, but never mixes those queries across commits.
        if who is None:
            return build_snapshot(who, qr)
        connection = db()
        connection.execute("BEGIN")
        try:
            return build_snapshot(who, qr)
        finally:
            connection.rollback()  # read-only transaction; GET still writes no rows

    def build_snapshot(who=None, qr=None):
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
            profile = db().execute("SELECT display_name FROM customer_profiles WHERE customer_id=?",
                                   (who["customer_id"],)).fetchone()
            result.update(csrf=who["csrf"], customer={
                "registered": profile is not None,
                "display_name": profile["display_name"] if profile is not None else None,
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
        return render_template("index.html", notice_version=NOTICE_VERSION, registration_open=app.contact_enabled,
                               catalog=event["products"] if event["id"] == DEMO_CATALOG_EVENT else ())

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
        if identity():
            return jsonify(snapshot(require_identity()))
        raise APIError("새 스탬프 카드를 등록하거나 기존 스탬프 찾기를 이용해 주세요.",
                       401, "registration_required")

    def phone_lookup(value):
        phone = normalized_phone(value)
        return hmac.digest(profile_key, phone.encode("ascii"), "sha256").hex()

    def profile_input(data):
        limited(("profile_ip", request.remote_addr), 120)
        if identity():
            require_identity()
            raise APIError("이 브라우저에는 이미 사용 중인 스탬프 카드가 있습니다.", 409, "active_session")
        raw_name = data.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not 1 <= len(name) <= 80 or not name.isprintable():
            raise APIError("이름을 1~80자로 입력해 주세요.")
        number = normalized_phone(data.get("phone"))
        lookup = hmac.digest(profile_key, number.encode("ascii"), "sha256").hex()
        limited(("profile_number", lookup), 10)
        return name, lookup, number

    @app.post("/api/register")
    def register():
        require_active()
        data = body()
        name, lookup, number = profile_input(data)
        if data.get("notice_acknowledged") is not True:
            raise APIError("개인정보 수집·이용 안내를 확인해 주세요.")
        if enrollment is None:
            raise APIError("운영 정책 확정 전에는 신규 가입을 받지 않습니다.", 403, "registration_closed")
        if data.get("notice_version") != NOTICE_VERSION:
            raise APIError("새 안내를 확인하고 다시 등록해 주세요.", 409, "notice_changed")
        if contact.profile_tag(name, number) not in enrollment.allowed_profiles:
            raise APIError("지정된 합성 정보만 등록할 수 있습니다.", 403, "synthetic_only")
        with write_transaction() as connection:
            require_active()
            if connection.execute("SELECT 1 FROM customer_profiles WHERE phone_lookup_hmac=?",
                                  (lookup,)).fetchone():
                raise APIError("이미 등록된 스탬프가 있어요. 기존 스탬프 찾기로 이동해 주세요. "
                               "이름 정정은 매장에 문의해 주세요.", 409, "phone_registered")
            customer_id, now = secrets.token_hex(16), int(time.time())
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO customers VALUES (?, ?, NULL)", (customer_id, now))
            connection.execute("INSERT INTO customer_profiles "
                               "(customer_id, display_name, phone_lookup_hmac, notice_version, notice_acknowledged_at) "
                               "VALUES (?, ?, ?, ?, ?)", (customer_id, name, lookup, NOTICE_VERSION, now))
            enrollment.store(connection, customer_id, number, now)
            token = new_session(customer_id)
        return set_cookie(jsonify(ok=True), token)

    @app.post("/api/find")
    def find():
        name, lookup, _ = profile_input(body())
        with write_transaction() as connection:
            target = connection.execute("SELECT customer_id FROM customer_profiles "
                                        "WHERE phone_lookup_hmac=? AND display_name=?", (lookup, name)).fetchone()
            if target is None:
                raise APIError("이름과 휴대전화번호를 확인해 주세요. 정정이 필요하면 매장에 문의해 주세요.",
                               404, "profile_not_found")
            token = new_session(target["customer_id"])
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
        with write_transaction():
            who = require_identity()
            require_active()
            redeemed = db().execute("SELECT redeemed_at FROM customers WHERE id=?", (who["customer_id"],)).fetchone()[0]
            added = False
            if not redeemed:
                cursor = db().execute("INSERT OR IGNORE INTO stamps VALUES (?, ?, ?)",
                                      (who["customer_id"], product["id"], int(time.time())))
                added = cursor.rowcount == 1
        return jsonify(**snapshot(who), added=added, product_id=product["id"])

    @app.post("/api/recovery-link")
    def recovery_link():
        who = require_identity()
        if body().get("confirm_rotation") is not True:
            raise APIError("기존 링크가 무효가 되는 것을 확인해 주세요.")
        limited(("issue_recovery", who["customer_id"]), 10)
        token = secrets.token_urlsafe(32)
        expiry = int(time.time()) + LIFETIME
        with write_transaction():
            who = require_identity()
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
        with write_transaction():
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
        return set_cookie(jsonify(ok=True), cookie)

    @app.post("/api/redeem")
    def redeem():
        who = require_identity()
        if body().get("confirm_received") is not True:
            raise APIError("실물 보상을 지급한 뒤 확인해 주세요.")
        limited(("redeem", who["customer_id"]), 20)
        with write_transaction():
            who = require_identity()
            customer_id = who["customer_id"]
            row = db().execute("SELECT redeemed_at FROM customers WHERE id=?", (customer_id,)).fetchone()
            already_redeemed = row[0] is not None
            if not already_redeemed:
                if not reward_open():
                    raise APIError("보상 지급 기간이 아닙니다. 직원에게 문의해 주세요.", 409, "reward_closed")
                count = db().execute("SELECT COUNT(*) FROM stamps WHERE customer_id=?", (customer_id,)).fetchone()[0]
                if count != 10:
                    raise APIError("10개 상품을 모두 모아야 보상을 받을 수 있습니다.", 409, "incomplete")
                now = int(time.time())
                db().execute("UPDATE customers SET redeemed_at=? WHERE id=? AND redeemed_at IS NULL", (now, customer_id))
                db().execute("INSERT INTO reward_audit VALUES (?, ?, 'unauthenticated_customer_screen')", (customer_id, now))
        return jsonify(**snapshot(who), already_redeemed=already_redeemed)

    install_admin(app, dev=dev, get_db=db, write_transaction=write_transaction, staff_window=staff_window,
                  phone_lookup=phone_lookup, json_body=body, api_error=APIError)
    return app
