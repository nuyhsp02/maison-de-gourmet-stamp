"""Staff-only routes for one Waitress process and one event database."""
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from flask import jsonify, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

SESSION_SECONDS = 8 * 3600
PASSWORD_METHOD = "pbkdf2:sha256:1000000"
ACTOR = re.compile(r"[A-Za-z0-9_.-]{1,64}")
PASSWORD_HASH = re.compile(re.escape(PASSWORD_METHOD) + r"\$[A-Za-z0-9]{16}\$[0-9a-f]{64}")


def validate_admin_schema(db):
    """An earlier DB has none of these objects. A partial PR2 DB needs its backup."""
    marker = db.execute("SELECT value FROM metadata WHERE key='admin_schema_v1'").fetchone()
    expected = {("table", "admin_sessions"), ("table", "stamp_cancel_audit"), ("trigger", "protect_redeemed_stamp")}
    found = {tuple(row) for row in db.execute(
        "SELECT type,name FROM sqlite_master WHERE name IN ('admin_sessions','stamp_cancel_audit','protect_redeemed_stamp')")}
    if marker is None and not found:
        return
    if marker is None or marker[0] != "1" or found != expected:
        raise ValueError("관리자 테이블 또는 취소 기록 구성이 다릅니다. 원본 백업을 확인하세요.")


def staff_key_path(database):
    return Path(str(database) + ".admin.key")


def write_staff_password(database, actor, password):
    """Provision/rotate a local verifier atomically, without opening the database."""
    if not ACTOR.fullmatch(actor):
        raise ValueError("계정 ID는 영문·숫자·마침표·밑줄·하이픈 1~64자로 지정하세요.")
    if not 12 <= len(password) <= 256 or not password.isprintable():
        raise ValueError("비밀번호는 표시 가능한 문자 12~256자로 지정하세요.")
    record = {"actor": actor, "password_hash": generate_password_hash(password, method=PASSWORD_METHOD)}
    path = staff_key_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates an owner-only file on POSIX. On Windows, inherit the private data directory ACL.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".staff-", suffix=".key")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_credentials(path):
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.loads(stream.read(2048))
        actor, verifier = data["actor"], data["password_hash"]
        if isinstance(actor, str) and ACTOR.fullmatch(actor) and isinstance(verifier, str) and PASSWORD_HASH.fullmatch(verifier):
            return actor, verifier
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def install_admin(app, *, dev, get_db, write_transaction, phone_lookup, json_body, api_error):
    cookie_name = "stamp_admin_dev" if dev else "__Host-stamp-admin"
    key_path = staff_key_path(app.config["DATABASE"])
    # Only the process holds this key. Even a restored DB containing a revoked
    # session cannot authenticate its old cookie when the server restarts.
    boot_key = secrets.token_bytes(32)
    products = {item["id"]: item["name"] for item in app.event["products"]}

    def token_hash(token, credentials):
        actor, verifier = credentials
        return hmac.digest(boot_key, f"{actor}\0{verifier}\0{token}".encode("ascii"), "sha256").hex()

    def session(connection, *, csrf):
        cookie = request.cookies.get(cookie_name, "")
        credentials = read_credentials(key_path)
        row = None
        if credentials and re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
            row = connection.execute(
                "SELECT * FROM admin_sessions WHERE token_hash=? AND expires_at>?",
                (token_hash(cookie, credentials), int(time.time()))).fetchone()
        if row is None:
            raise api_error("직원 로그인이 필요합니다.", 401, "admin_login_required")
        if csrf:
            supplied = request.headers.get("X-Admin-CSRF", "")
            if not supplied.isascii() or not hmac.compare_digest(supplied, row["csrf"]):
                raise api_error("관리자 화면을 새로고침한 뒤 다시 시도하세요.", 403, "admin_csrf_failed")
        return row

    @app.get("/admin")
    def admin_page():
        return render_template("admin.html")

    @app.post("/api/admin/login")
    def admin_login():
        credentials = read_credentials(key_path)
        if credentials is None:
            raise api_error("직원 로그인 설정을 확인해 주세요.", 503, "admin_unavailable")
        # Count attempts before expensive password verification, including concurrent attempts.
        if not app.limits.allow(("staff_login", request.remote_addr), 5) or not app.limits.allow(("staff_login_all",), 30):
            raise api_error("로그인 요청이 많습니다. 1분 뒤 다시 시도하세요.", 429, "rate_limited")
        password = json_body().get("password")
        if (not isinstance(password, str) or not 1 <= len(password) <= 256 or not password.isprintable()
                or not check_password_hash(credentials[1], password)):
            raise api_error("비밀번호를 확인해 주세요.", 401, "admin_login_failed")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with write_transaction() as db:
            if read_credentials(key_path) != credentials:
                raise api_error("직원 로그인 설정이 변경되었습니다. 다시 로그인하세요.", 401, "admin_login_failed")
            now = int(time.time())
            expires = now + SESSION_SECONDS
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now,))
            previous = request.cookies.get(cookie_name, "")
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", previous):
                db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(previous, credentials),))
            db.execute("INSERT INTO admin_sessions(token_hash, csrf, admin_actor, created_at, expires_at) VALUES (?,?,?,?,?)",
                       (token_hash(token, credentials), csrf, credentials[0], now, expires))
        response = jsonify(actor=credentials[0], csrf=csrf, expires_at=expires)
        response.set_cookie(cookie_name, token, max_age=SESSION_SECONDS, httponly=True, secure=not dev,
                            samesite="Strict", path="/")
        return response

    @app.get("/api/admin/session")
    def admin_session():
        row = session(get_db(), csrf=False)
        return jsonify(actor=row["admin_actor"], csrf=row["csrf"], expires_at=row["expires_at"])

    @app.post("/api/admin/logout")
    def admin_logout():
        session(get_db(), csrf=True)
        with write_transaction() as db:
            row = session(db, csrf=True)
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (row["token_hash"],))
        response = jsonify(ok=True)
        response.delete_cookie(cookie_name, path="/", secure=not dev, httponly=True, samesite="Strict")
        return response

    @app.get("/api/admin/stats")
    def admin_stats():
        db = get_db()
        session(db, csrf=False)
        rows = db.execute("""
            WITH cards AS (
                SELECT c.id, c.redeemed_at, COUNT(s.product_id) AS held FROM customers c
                LEFT JOIN stamps s ON s.customer_id=c.id GROUP BY c.id
            )
            SELECT held, COUNT(*) AS cards, COUNT(redeemed_at) AS redeemed FROM cards GROUP BY held
        """)
        distribution = [0] * (len(products) + 1)
        redeemed = 0
        for row in rows:
            distribution[row["held"]] = row["cards"]
            redeemed += row["redeemed"]
        return jsonify(cards=sum(distribution), stamps=sum(n * count for n, count in enumerate(distribution)),
                       redeemed=redeemed, distribution=distribution)

    def customer_id(data):
        value = data.get("customer_id")
        if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
            raise api_error("카드 ID를 확인해 주세요.")
        return value

    @app.post("/api/admin/search")
    def admin_search():
        db = get_db()
        session(db, csrf=True)
        data = json_body()
        if set(data) == {"phone"}:
            predicate, value = "p.phone_lookup_hmac=?", phone_lookup(data["phone"])
        elif set(data) == {"customer_id"}:
            predicate, value = "c.id=?", customer_id(data)
        else:
            raise api_error("카드 ID 또는 휴대전화번호 한 가지를 입력해 주세요.")
        # One SELECT gives the profile, redemption and all ten products one read snapshot.
        rows = db.execute("""
            SELECT c.id, p.display_name, c.redeemed_at, item.id AS product_id, s.collected_at,
                   COALESCE((SELECT MAX(a.id) FROM stamp_cancel_audit a
                             WHERE a.customer_id=c.id AND a.product_id=item.id), 0) AS revision
            FROM customers c LEFT JOIN customer_profiles p ON p.customer_id=c.id
            CROSS JOIN products item
            LEFT JOIN stamps s ON s.customer_id=c.id AND s.product_id=item.id
            WHERE """ + predicate, (value,)).fetchall()
        if not rows:
            raise api_error("일치하는 카드를 찾을 수 없습니다.", 404, "card_not_found")
        by_product = {row["product_id"]: row for row in rows}
        stamps = [dict(product_id=pid, name=name, collected_at=by_product[pid]["collected_at"],
                       revision=by_product[pid]["revision"]) for pid, name in products.items()]
        return jsonify(customer_id=rows[0]["id"], name=rows[0]["display_name"],
                       redeemed_at=rows[0]["redeemed_at"], stamps=stamps)

    @app.post("/api/admin/cancel")
    def admin_cancel():
        session(get_db(), csrf=True)
        data = json_body()
        cid, pid, revision = customer_id(data), data.get("product_id"), data.get("revision")
        if not isinstance(pid, str) or pid not in products:
            raise api_error("해당 상품을 찾을 수 없습니다.", 404, "product_not_found")
        if type(revision) is not int or revision < 0:
            raise api_error("취소 대상을 다시 확인해 주세요.")
        with write_transaction() as db:
            who = session(db, csrf=True)
            card = db.execute("SELECT redeemed_at FROM customers WHERE id=?", (cid,)).fetchone()
            if card is None:
                raise api_error("해당 카드를 찾을 수 없습니다.", 404, "card_not_found")
            if card["redeemed_at"] is not None:
                raise api_error("보상을 수령한 카드의 스탬프는 취소할 수 없습니다.", 409, "already_redeemed")
            stamp = db.execute("SELECT collected_at FROM stamps WHERE customer_id=? AND product_id=?", (cid, pid)).fetchone()
            # A cancelled stamp can be collected again, even in the same second.
            # Its cancellation revision distinguishes it from the stamp the operator saw.
            current = db.execute("SELECT COALESCE(MAX(id),0) FROM stamp_cancel_audit WHERE customer_id=? AND product_id=?",
                                 (cid, pid)).fetchone()[0]
            if stamp is None or revision != current:
                raise api_error("스탬프 상태가 바뀌었습니다. 카드를 다시 조회해 주세요.", 409, "stamp_changed")
            db.execute("DELETE FROM stamps WHERE customer_id=? AND product_id=?", (cid, pid))
            db.execute("""INSERT INTO stamp_cancel_audit
                (customer_id, product_id, original_collected_at, cancelled_at, admin_actor)
                VALUES (?,?,?,?,?)""", (cid, pid, stamp["collected_at"], int(time.time()), who["admin_actor"]))
        return jsonify(ok=True)
