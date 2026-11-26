"""Staff-only routes for one Waitress process and one event database."""
from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
import unicodedata
from pathlib import Path

from flask import jsonify, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

if __package__:
    from .correction_schema import correction_enabled
else:
    from correction_schema import correction_enabled

MANAGER_BINDING = "manager_credential_v1"
PROD_STAFF_BINDING = "prod_staff_credential_v1"
SESSION_SECONDS = 8 * 3600
PASSWORD_METHOD = "pbkdf2:sha256:1000000"
ACTOR = re.compile(r"[A-Za-z0-9_.-]{1,64}")
PASSWORD_HASH = re.compile(re.escape(PASSWORD_METHOD) + r"\$[A-Za-z0-9]{16}\$[0-9a-f]{64}")


def validate_admin_schema(db):
    """An earlier DB has none of these objects. A partial PR2 DB needs its backup."""
    corrections = correction_enabled(db)
    marker = db.execute("SELECT value FROM metadata WHERE key='admin_schema_v1'").fetchone()
    expected = {("table", "admin_sessions"), ("table", "stamp_cancel_audit"), ("trigger", "protect_redeemed_stamp")}
    found = {tuple(row) for row in db.execute(
        "SELECT type,name FROM sqlite_master WHERE name IN ('admin_sessions','stamp_cancel_audit','protect_redeemed_stamp')")}
    if marker is None and not found and not corrections:
        return
    if marker is None or marker[0] != "1" or found != expected:
        raise ValueError("관리자 테이블 또는 취소 기록 구성이 다릅니다. 원본 백업을 확인하세요.")


def staff_key_path(database):
    return Path(str(database) + ".admin.key")


def manager_key_path(database):
    return Path(str(database) + ".manager.key")


def overlapping_credentials(actor, password, other):
    return other is not None and (actor.casefold() == other[0].casefold() or check_password_hash(other[1], password))


def manager_fingerprint(credentials):
    # Each issuance uses a newly salted verifier. Rollback of the file alone
    # cannot match the currently active generation stored in the database.
    actor, verifier = credentials
    return hashlib.sha256(f"maison/manager/v1\0{actor}\0{verifier}".encode("ascii")).hexdigest()


def retire_restored_manager(db):
    """Disarm a restore candidate inside the caller's transaction; preserve staff and customer sessions."""
    changed = db.execute("UPDATE metadata SET value='revoked' WHERE key=?", (MANAGER_BINDING,))
    if changed.rowcount:
        db.execute("DELETE FROM admin_sessions WHERE token_hash GLOB 'manager:*'")


def prod_instance(db):
    markers = dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('linux_prod_v1','prod_instance_id')"))
    if not markers:
        return None
    instance = markers.get("prod_instance_id", "")
    if markers.get("linux_prod_v1") != "1" or not re.fullmatch(r"[a-f0-9]{64}", instance):
        raise ValueError("PROD 자격을 연결할 DB 식별 정보가 올바르지 않습니다.")
    return instance


def is_prod_database(database):
    path = Path(database)
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
                    and prod_instance(db))


def retire_prod_credentials(db):
    if not prod_instance(db):
        raise ValueError("PROD 복원 후보를 확인하세요.")
    db.execute("UPDATE metadata SET value='revoked' WHERE key IN (?,?)", (MANAGER_BINDING, PROD_STAFF_BINDING))
    db.execute("DELETE FROM admin_sessions")


def bound_prod_verifier(db, database, role, instance):
    path = manager_key_path(database) if role == "admin" else staff_key_path(database)
    credentials = read_credentials(path, private=True, scope=instance + "/" + role)
    binding = MANAGER_BINDING if role == "admin" else PROD_STAFF_BINDING
    row = db.execute("SELECT value FROM metadata WHERE key=?", (binding,)).fetchone()
    if credentials and row and hmac.compare_digest(row[0], manager_fingerprint(credentials)):
        return credentials
    return None


def validate_prod_credentials(db, database):
    instance = prod_instance(db)
    if not instance:
        raise ValueError("PROD DB를 확인하세요.")
    staff = bound_prod_verifier(db, database, "staff", instance)
    manager = bound_prod_verifier(db, database, "admin", instance)
    if not staff or not manager or staff[0].casefold() == manager[0].casefold() or staff[1] == manager[1]:
        raise ValueError("이 PROD DB용 직원·관리자 자격을 별도로 발급하세요.")


@contextmanager
def manager_writer(database):
    # Never create a DB or run application startup/migrations during provisioning.
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        raise ValueError("이미 준비된 행사 DB 파일을 명시하세요.")
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM metadata WHERE key='admin_schema_v1' AND value='1'").fetchone():
                raise ValueError("직원 관리 스키마가 있는 행사 DB가 필요합니다.")
            validate_admin_schema(db)
            yield db
    except sqlite3.Error:
        raise ValueError("관리자 자격 상태를 저장할 수 없습니다. DB와 검증 파일을 확인하세요.") from None


def revoke_manager(database):
    with manager_writer(database) as db:
        retire_restored_manager(db)
        manager_key_path(database).unlink(missing_ok=True)


def minimum_password_length(*, production):
    return 4 if production else 15


def write_staff_password(database, actor, password, *, role="staff", production=False):
    """Provision a staff verifier or rotate the manager's file and DB binding."""
    if not ACTOR.fullmatch(actor):
        raise ValueError("계정 ID는 영문·숫자·마침표·밑줄·하이픈 1~64자로 지정하세요.")
    minimum = minimum_password_length(production=production)
    if not minimum <= len(password) <= 256 or not password.isprintable():
        raise ValueError(f"비밀번호는 표시 가능한 문자 {minimum}~256자로 지정하세요.")
    normalized = re.sub(r"[^\w]", "", password.casefold()).replace("_", "")
    stems = ("password", "qwerty", "letmein", "administrator", "maisondegourmet",
             "gourmetstamp", "iloveyou", "비밀번호", re.sub(r"\W", "", actor.casefold()))
    common = {"correcthorsebatterystaple", "thisismypassword", "letmeinplease", "changemypassword"}
    sequences = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl")
    predictable = any(normalized in (sequence * 26) or normalized in (sequence[::-1] * 26)
                      for sequence in sequences)
    if (not normalized or predictable or normalized in common or len(set(normalized)) <= 3
            or any(re.fullmatch(re.escape(stem) + r"[0-9]*", normalized) for stem in stems)
            or re.fullmatch(r"(.{1,10})\1+", normalized)):
        raise ValueError("흔하거나 반복되는 비밀번호입니다. 새로운 긴 문구를 입력하세요.")
    paths = {"staff": staff_key_path(database), "admin": manager_key_path(database)}
    if role not in paths:
        raise ValueError("알 수 없는 계정 역할입니다.")
    path = paths[role]
    other_path = paths["staff" if role == "admin" else "admin"]
    if production:
        with manager_writer(database) as db:
            instance = prod_instance(db)
            if not instance:
                raise ValueError("PROD 행사 DB를 명시하세요.")
            peer_role = "staff" if role == "admin" else "admin"
            peer = read_credentials(other_path, private=True, scope=instance + "/" + peer_role)
            if (other_path.exists() and not peer) or overlapping_credentials(actor, password, peer):
                raise ValueError("같은 PROD DB의 별개 직원·관리자 자격을 확인하세요.")
            scope = instance + "/" + role
            old = read_credentials(path, private=True, scope=scope)
            if old and check_password_hash(old[1], password):
                raise ValueError("기존 비밀번호와 다른 비밀번호를 설정하세요.")
            verifier = generate_password_hash(password, method=PASSWORD_METHOD)
            binding = MANAGER_BINDING if role == "admin" else PROD_STAFF_BINDING
            db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (binding, manager_fingerprint((actor, verifier))))
            if role == "admin":
                db.execute("DELETE FROM admin_sessions WHERE token_hash GLOB 'manager:*'")
            else:
                db.execute("DELETE FROM admin_sessions WHERE token_hash NOT GLOB 'manager:*'")
            save_verifier(path, {"actor": actor, "password_hash": verifier, "prod_scope": scope})
        return
    if is_prod_database(database):
        raise ValueError("PROD 전용 자격 발급 명령을 사용하세요.")
    other = read_credentials(other_path, private=(role == "staff"))
    if other is None and other_path.exists():
        raise ValueError("다른 역할의 로그인 설정을 확인하세요.")
    if overlapping_credentials(actor, password, other):
        raise ValueError("직원과 관리자는 서로 다른 계정 ID와 비밀번호를 사용하세요.")
    record = {"actor": actor, "password_hash": generate_password_hash(password, method=PASSWORD_METHOD)}
    if role == "admin":
        with manager_writer(database) as db:
            previous = read_credentials(path, private=True)
            if previous and check_password_hash(previous[1], password):
                raise ValueError("기존 관리자 비밀번호와 다른 비밀번호를 설정하세요.")
            retire_restored_manager(db)
            db.execute("INSERT INTO metadata(key,value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (MANAGER_BINDING, manager_fingerprint((actor, record["password_hash"]))))
            # The DB write lock serializes issuance and authenticated mutations.
            # A crash between replace and commit leaves a mismatch (disabled).
            save_verifier(path, record)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_verifier(path, record)


def save_verifier(path, record):
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


def read_credentials(path, *, private=False, scope=None):
    try:
        if private:
            info = path.lstat()
            mode = info.st_mode
            if not stat.S_ISREG(mode) or (os.name == "posix" and stat.S_IMODE(mode) != 0o600):
                return None
            if scope and (info.st_nlink != 1 or info.st_uid != os.geteuid()):
                return None
        with path.open(encoding="utf-8") as stream:
            data = json.loads(stream.read(2048))
        actor, verifier = data["actor"], data["password_hash"]
        if data.get("prod_scope") != scope:
            return None
        if isinstance(actor, str) and ACTOR.fullmatch(actor) and isinstance(verifier, str) and PASSWORD_HASH.fullmatch(verifier):
            return actor, verifier
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def install_admin(app, *, dev, get_db, write_transaction, staff_window, phone_lookup, json_body, api_error):
    cookie_name = "stamp_admin_dev" if dev else "__Host-stamp-admin"
    if getattr(app, "production", False):
        cookie_name = "__Host-maison-prod-staff"
    paths = {"staff": staff_key_path(app.config["DATABASE"]), "admin": manager_key_path(app.config["DATABASE"])}
    # Only the process holds this key. Even a restored DB containing a revoked
    # session cannot authenticate its old cookie when the server restarts.
    boot_key = secrets.token_bytes(32)
    products = {item["id"]: item["name"] for item in app.event["products"]}

    def verifier(role, db):
        if getattr(app, "production", False):
            instance = prod_instance(db)
            return bound_prod_verifier(db, app.config["DATABASE"], role, instance) if instance else None
        credentials = read_credentials(paths[role], private=(role == "admin"))
        if role == "admin" and credentials is not None:
            active = db.execute("SELECT value FROM metadata WHERE key=?", (MANAGER_BINDING,)).fetchone()
            if active is None or active[0] != manager_fingerprint(credentials):
                return None
        return credentials

    def token_hash(token, role, credentials):
        actor, verifier = credentials
        digest = hmac.digest(boot_key, f"{role}\0{actor}\0{verifier}\0{token}".encode("ascii"), "sha256").hex()
        return "manager:" + digest if role == "admin" else digest

    def session(connection, *, csrf, manager=False):
        cookie = request.cookies.get(cookie_name, "")
        row = None
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
            for role in paths:
                credentials = verifier(role, connection)
                if credentials:
                    row = connection.execute(
                        """SELECT token_hash, csrf, admin_actor, expires_at, ? AS role FROM admin_sessions
                           WHERE token_hash=? AND admin_actor=? AND expires_at>?""",
                        (role, token_hash(cookie, role, credentials), credentials[0], int(time.time()))).fetchone()
                    if row is not None:
                        break
        if row is None:
            raise api_error("직원 로그인이 필요합니다.", 401, "admin_login_required")
        if manager and row["role"] != "admin":
            raise api_error("관리자 권한이 필요합니다.", 403, "admin_required")
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
        return login("staff")

    @app.post("/api/admin/manager-login")
    def manager_login():
        return login("admin")

    def login(role):
        credentials = verifier(role, get_db())
        if credentials is None:
            raise api_error("직원 로그인 설정을 확인해 주세요.", 503, "admin_unavailable")
        # Count attempts before expensive password verification, including concurrent attempts.
        if not app.limits.allow(("staff_login", request.remote_addr), 5) or not app.limits.allow(("staff_login_all",), 30):
            raise api_error("로그인 요청이 많습니다. 1분 뒤 다시 시도하세요.", 429, "rate_limited")
        data = json_body()
        if set(data) != {"password"}:
            raise api_error("비밀번호만 입력해 주세요.")
        password = data["password"]
        if (not isinstance(password, str) or not 1 <= len(password) <= 256 or not password.isprintable()
                or not check_password_hash(credentials[1], password)):
            raise api_error("비밀번호를 확인해 주세요.", 401, "admin_login_failed")
        # Fail closed if both verifier files were provisioned with the same identity or password.
        if role == "admin":
            staff = verifier("staff", get_db())
            if (staff is None and paths["staff"].exists()) or overlapping_credentials(credentials[0], password, staff):
                raise api_error("관리자 로그인 설정을 확인해 주세요.", 503, "admin_unavailable")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with write_transaction() as db:
            if verifier(role, db) != credentials or (role == "admin" and verifier("staff", db) != staff):
                raise api_error("직원 로그인 설정이 변경되었습니다. 다시 로그인하세요.", 401, "admin_login_failed")
            now = int(time.time())
            expires = now + SESSION_SECONDS
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now,))
            previous = request.cookies.get(cookie_name, "")
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", previous):
                for old_role in paths:
                    old_credentials = verifier(old_role, db)
                    if old_credentials:
                        db.execute("DELETE FROM admin_sessions WHERE token_hash=?",
                                   (token_hash(previous, old_role, old_credentials),))
            db.execute("INSERT INTO admin_sessions(token_hash, csrf, admin_actor, created_at, expires_at) VALUES (?,?,?,?,?)",
                       (token_hash(token, role, credentials), csrf, credentials[0], now, expires))
        response = session_response(credentials[0], role, csrf, expires)
        response.set_cookie(cookie_name, token, max_age=SESSION_SECONDS, httponly=True, secure=not dev,
                            samesite="Strict", path="/")
        return response

    @app.get("/api/admin/session")
    def admin_session():
        row = session(get_db(), csrf=False)
        return session_response(row["admin_actor"], row["role"], row["csrf"], row["expires_at"])

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

    def session_response(actor, role, csrf, expires_at):
        now = time.time()
        period, transition = staff_window(now)
        return jsonify(actor=actor, role=role, csrf=csrf, expires_at=expires_at, server_now=now,
                       period=period, next_transition_at=transition)

    def card_revision_sql(enabled):
        # Adding a stamp advances the revision. A cancellation/removal decreases
        # held by one but adds two via its audit row. Replacement advances by two.
        corrections = "(SELECT COUNT(*) FROM stamp_correction_audit WHERE customer_id=c.id)" if enabled else "0"
        return f"""(SELECT COUNT(*) FROM stamps WHERE customer_id=c.id)
                   + 2 * ((SELECT COUNT(*) FROM stamp_cancel_audit WHERE customer_id=c.id) + {corrections})"""

    def correction_schema(db):
        try:
            return correction_enabled(db)
        except ValueError:
            raise api_error("정정 감사 기록 구성을 확인해 주세요.", 503, "correction_unavailable") from None

    @app.post("/api/admin/search")
    def admin_search():
        db = get_db()
        who = session(db, csrf=True)
        data = json_body()
        if set(data) == {"phone"}:
            predicate, value = "p.phone_lookup_hmac=?", phone_lookup(data["phone"])
        elif set(data) == {"customer_id"}:
            predicate, value = "c.id=?", customer_id(data)
        else:
            raise api_error("카드 ID 또는 휴대전화번호 한 가지를 입력해 주세요.")
        # One SELECT gives the profile, redemption and all ten products one read snapshot.
        mask_column = "contact.phone_masked" if app.contact_enabled else "NULL"
        contact_join = "LEFT JOIN customer_contacts contact ON contact.customer_id=c.id" if app.contact_enabled else ""
        enabled = correction_schema(db)
        rows = db.execute(f"""
            SELECT c.id, p.display_name, {mask_column} AS phone_masked, c.redeemed_at, item.id AS product_id, s.collected_at,
                   {card_revision_sql(enabled)} AS correction_revision,
                   COALESCE((SELECT MAX(a.id) FROM stamp_cancel_audit a
                             WHERE a.customer_id=c.id AND a.product_id=item.id), 0) AS revision
            FROM customers c LEFT JOIN customer_profiles p ON p.customer_id=c.id
            {contact_join}
            CROSS JOIN products item
            LEFT JOIN stamps s ON s.customer_id=c.id AND s.product_id=item.id
            WHERE """ + predicate, (value,)).fetchall()
        if not rows:
            raise api_error("일치하는 카드를 찾을 수 없습니다.", 404, "card_not_found")
        by_product = {row["product_id"]: row for row in rows}
        stamps = [dict(product_id=pid, name=name, collected_at=by_product[pid]["collected_at"],
                       revision=by_product[pid]["revision"]) for pid, name in products.items()]
        mask = rows[0]["phone_masked"]
        if mask is not None and (not isinstance(mask, str) or not re.fullmatch(r"01[016789]-\*{3,4}-[0-9]{4}", mask)):
            raise api_error("연락처 표시 형식을 확인할 수 없습니다.", 503, "contact_unavailable")
        current_period, _ = staff_window(time.time())
        held = sum(stamp["collected_at"] is not None for stamp in stamps)
        unredeemed = rows[0]["redeemed_at"] is None
        permissions = dict(period=current_period, can_cancel=current_period == "collection" and unredeemed and held > 0,
                           can_correct=enabled and who["role"] == "admin" and current_period == "correction"
                           and unredeemed and 0 < held < len(products))
        return jsonify(customer_id=rows[0]["id"], name=rows[0]["display_name"],
                       permissions=permissions, correction_revision=rows[0]["correction_revision"],
                       phone_masked=mask, redeemed_at=rows[0]["redeemed_at"], stamps=stamps)

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
            cancelled_at = int(time.time())
            if staff_window(cancelled_at)[0] != "collection":
                raise api_error("적립 기간이 종료되어 스탬프를 취소할 수 없습니다.", 409, "cancel_closed")
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
                VALUES (?,?,?,?,?)""", (cid, pid, stamp["collected_at"], cancelled_at, who["admin_actor"]))
        return jsonify(ok=True)

    def correction_reason(data, display_name):
        reason = data.get("reason_detail")
        if not isinstance(reason, str) or not reason.isprintable() or not 10 <= len(reason.strip()) <= 300:
            raise api_error("개인정보 없이 정정 사유를 10~300자로 입력해 주세요.")
        reason = reason.strip()
        normalized = unicodedata.normalize("NFKC", reason).casefold()
        name = unicodedata.normalize("NFKC", display_name or "").casefold()
        if ((name and name in normalized)
                or re.search(r"(?:\+?82|0)[\s().-]*[1-9](?:[\s().-]*\d){7,9}", normalized)
                or re.search(r"\d{6}[- ]?[1-8]\d{6}|[^\s@]+@[^\s@]+\.[^\s@]+", normalized)):
            raise api_error("정정 사유에서 이름과 전화번호 등 개인정보를 제외해 주세요.")
        return reason

    @app.post("/api/admin/correct")
    def admin_correct():
        session(get_db(), csrf=True, manager=True)
        data = json_body()
        if set(data) != {"request_id", "customer_id", "product_id", "revision", "type", "new_product_id",
                         "reason_code", "reason_detail"}:
            raise api_error("정정 요청 항목을 확인해 주세요.")
        cid, pid, new_pid = customer_id(data), data["product_id"], data["new_product_id"]
        request_id, revision, kind = data["request_id"], data["revision"], data["type"]
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise api_error("정정 요청 ID를 확인해 주세요.")
        if type(revision) is not int or revision < 0:
            raise api_error("카드를 다시 조회해 주세요.")
        if not isinstance(pid, str) or pid not in products:
            raise api_error("해당 상품을 찾을 수 없습니다.", 404, "product_not_found")
        if kind == "replace":
            if not isinstance(new_pid, str) or new_pid not in products or new_pid == pid:
                raise api_error("다른 행사 상품을 선택해 주세요.")
            reason_code = "wrong_product"
        elif kind == "remove" and new_pid is None:
            reason_code = "erroneous_stamp"
        else:
            raise api_error("허용된 정정 유형을 선택해 주세요.")
        if data["reason_code"] != reason_code:
            raise api_error("정정 유형과 사유를 확인해 주세요.")

        with write_transaction() as db:
            who = session(db, csrf=True, manager=True)
            now = int(time.time())
            if staff_window(now)[0] != "correction":
                raise api_error("관리자 정정 기간이 아닙니다.", 409, "correction_closed")
            if not correction_schema(db):
                raise api_error("정정 감사 기록 설정이 필요합니다.", 503, "correction_unavailable")
            card = db.execute(f"""SELECT c.redeemed_at, p.display_name, {card_revision_sql(True)} AS revision
                                  FROM customers c LEFT JOIN customer_profiles p ON p.customer_id=c.id
                                  WHERE c.id=?""", (cid,)).fetchone()
            if card is None:
                raise api_error("해당 카드를 찾을 수 없습니다.", 404, "card_not_found")
            if card["redeemed_at"] is not None:
                raise api_error("보상을 수령한 카드는 정정할 수 없습니다.", 409, "already_redeemed")
            reason = correction_reason(data, card["display_name"])
            if db.execute("SELECT 1 FROM stamp_correction_audit WHERE request_id=?", (request_id,)).fetchone():
                raise api_error("이미 처리된 요청입니다. 카드를 다시 조회해 주세요.", 409, "duplicate_correction")
            held = dict(db.execute("SELECT product_id, collected_at FROM stamps WHERE customer_id=?", (cid,)))
            eligible = len(held) == len(products)
            if eligible:
                raise api_error("완성 카드는 보호됩니다. 운영자에게 검토를 요청하세요.", 409, "protected_complete")
            if revision != card["revision"] or pid not in held or new_pid in held:
                raise api_error("스탬프 상태를 다시 확인해 주세요.", 409, "stamp_changed")
            if kind == "replace":
                changed = db.execute("UPDATE stamps SET product_id=? WHERE customer_id=? AND product_id=?",
                                     (new_pid, cid, pid))
            else:
                changed = db.execute("DELETE FROM stamps WHERE customer_id=? AND product_id=?", (cid, pid))
            count = db.execute("SELECT COUNT(*) FROM stamps WHERE customer_id=?", (cid,)).fetchone()[0]
            eligible_after = count == len(products)
            if changed.rowcount != 1 or count != len(held) - int(kind == "remove") or eligible != eligible_after:
                raise api_error("스탬프 수와 보상 자격을 보존할 수 없습니다.", 409, "stamp_changed")
            audit = db.execute("""INSERT INTO stamp_correction_audit
                (request_id, customer_id, admin_actor, corrected_at, correction_type, reason_code, reason_detail,
                 old_product_id, new_product_id, original_collected_at, stamp_count_after, eligible_before, eligible_after)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (request_id, cid, who["admin_actor"], now, kind, reason_code, reason, pid, new_pid,
                                held[pid], count, eligible, eligible_after))
            if audit.rowcount != 1:
                raise api_error("정정 이력을 기록할 수 없습니다.", 503, "storage_unavailable")
        return jsonify(ok=True, request_id=request_id)
