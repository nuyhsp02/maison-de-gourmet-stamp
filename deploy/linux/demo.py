#!/usr/bin/env python3
"""Prepare a new synthetic event or serve an existing, verified DEMO database."""
import argparse
from contextlib import closing
from functools import partial
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import urlsplit

APP_DIR = Path(__file__).resolve().parents[2] / "app"
sys.path.insert(0, str(APP_DIR))
from app import (connect_db, create_app, default_profile_key_path, event_signature,
                 init_db, load_event, verify_profile_key)
import contact_privacy as contact
from staff_admin import read_credentials, staff_key_path, validate_admin_schema


DATABASE_NAME = "collection.sqlite"
TABLES = {"metadata", "products", "customers", "customer_profiles", "sessions", "stamps",
          "recovery", "reward_audit", "admin_sessions", "stamp_cancel_audit"}


class DemoError(ValueError):
    """Operator-facing diagnostic that contains no customer or credential value."""


def validate_demo_event(event):
    if event.get("draft") is not False or not re.fullmatch(r"demo-[a-z0-9][a-z0-9-]{0,58}", event["id"]):
        raise DemoError("DEMO 전용 demo- 행사 ID와 draft=false가 필요합니다.")


def load_demo_event(path):
    try:
        event = load_event(path)
        validate_demo_event(event)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DemoError("DEMO 설정의 행사 ID(demo-), draft=false, 날짜와 상품 구성을 확인하세요.") from exc
    return event


def validate_origin(origin):
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    url = urlsplit(origin)
    host = url.hostname or ""
    if (not re.fullmatch(rf"{label}(?:\.{label})+", host) or len(host) > 253 or
            host.rsplit(".", 1)[-1].isdigit() or origin != f"https://{host}"):
        raise DemoError("원본 주소에 포트와 경로가 없는 소문자 HTTPS 도메인을 지정하세요.")


def check_private(path, *, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise DemoError(f"필수 {'디렉터리' if directory else '파일'} 누락: {path.name}") from exc
    if directory:
        valid = stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
    else:
        valid = stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    if not valid:
        raise DemoError(f"실제 디렉터리 0700, 일반 파일 0600 권한을 확인하세요: {path.name}")


def inventory_signature(db, event, profile_key):
    # The original PR2 schema (including indexes/trigger bodies) and QR set are immutable
    # for one event. Bind their bootstrap values to its private profile key.
    schema = [tuple(row) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]
    recipient = contact.contact_key_id(db)
    tables = TABLES | (contact.CONTACT_TABLES if recipient else set())
    if {name for kind, name, _ in schema if kind == "table"} != tables:
        raise DemoError("DEMO DB의 PR2 테이블 구성이 다릅니다.")
    products = [tuple(row) for row in db.execute("SELECT id,qr_key FROM products ORDER BY id")]
    if ([pid for pid, _ in products] != sorted(p["id"] for p in event["products"]) or
            any(not isinstance(qr, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", qr)
                for _, qr in products) or len({qr for _, qr in products}) != 10):
        raise DemoError("DEMO 상품 또는 보관된 QR 키 구성이 다릅니다.")
    payload = json.dumps([event_signature(event), schema, products], separators=(",", ":")).encode()
    label = b"maison/linux-demo/inventory/v1\0"
    if recipient:
        label = b"maison/linux-demo/inventory/v2\0" + recipient.encode("ascii") + b"\0"
    return hmac.digest(profile_key, label + payload, "sha256").hex()


def validate_database(database, event, *, require_wal=False):
    """Read a consistent existing DB + profile key. Also used on isolated backup copies."""
    database = Path(database)
    validate_demo_event(event)
    check_private(database.parent, directory=True)
    check_private(database)
    key_path = default_profile_key_path(database)
    check_private(key_path)
    with closing(connect_db(database, mode="ro")) as db:
        db.execute("BEGIN")
        return validate_rows(db, event, key_path, require_wal)


def validate_rows(db, event, key_path, require_wal=False):
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    if (metadata.get("linux_demo_v1") != "1" or metadata.get("admin_schema_v1") != "1" or
            metadata.get("event") != event_signature(event)):
        raise DemoError("DEMO 표시, 관리자 버전 또는 행사 서명이 일치하지 않습니다.")
    try:
        key = verify_profile_key(db, key_path)
    except ValueError as exc:
        raise DemoError("DEMO DB와 프로필 키가 일치하지 않거나 손상되었습니다.") from exc
    if key is None:
        raise DemoError("DEMO 프로필 키의 DB 확인값이 없습니다.")
    validate_admin_schema(db)
    recipient = contact.contact_key_id(db)
    if bool(recipient) != ("linux_demo_inventory_v2" in metadata):
        raise DemoError("연락처 스키마 버전과 검증 기록이 일치하지 않습니다.")
    sealed = metadata.get("linux_demo_inventory_v2" if recipient else "linux_demo_inventory_v1")
    if sealed != inventory_signature(db, event, key):
        raise DemoError("DEMO 스키마 또는 상품 QR 키가 최초 준비 기록과 다릅니다.")
    if require_wal and db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        raise DemoError("DEMO DB의 저널 모드가 WAL과 다릅니다.")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
        raise DemoError("DEMO DB 무결성 검사가 실패했습니다.")
    return key


def prepare(state_dir, config, origin):
    validate_origin(origin)
    event = load_demo_event(config)
    state_dir = Path(state_dir)
    check_private(state_dir, directory=True)
    if any(state_dir.iterdir()):
        raise DemoError("최초 준비에는 비어 있는 새 DEMO 디렉터리가 필요합니다.")
    target = state_dir / DATABASE_NAME
    key_target = default_profile_key_path(target)
    with tempfile.TemporaryDirectory(dir=state_dir, prefix=".prepare-") as temporary:
        database = Path(temporary) / DATABASE_NAME
        key = init_db(database, event)
        with closing(connect_db(database, mode="rw")) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("linux_demo_v1", "1"),
                ("linux_demo_inventory_v1", inventory_signature(db, event, key)),
            ])
        validate_database(database, event)
        # Same filesystem and no replacement. Publish the database only when its key
        # is already in place. A concurrent prepare can never replace either file.
        os.link(default_profile_key_path(database), key_target)
        try:
            os.link(database, target)
        except OSError:
            key_target.unlink()
            raise


def migrate_contacts(state_dir, config, origin, synthetic_config):
    validate_origin(origin)
    if not urlsplit(origin).hostname.endswith(".invalid"):
        raise DemoError("격리된 합성 .invalid 호스트에서만 이관할 수 있습니다.")
    event = load_demo_event(config)
    enrollment = contact.SyntheticEnrollment.read(synthetic_config, event["id"])
    database = Path(state_dir) / DATABASE_NAME
    check_private(database.parent, directory=True)
    check_private(database)
    key_path = default_profile_key_path(database)
    check_private(key_path)
    with closing(connect_db(database, mode="rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        key = validate_rows(db, event, key_path, require_wal=True)
        already = contact.contact_key_id(db)
        contact.apply_contact_schema(db, enrollment.key_id)
        if already is None:
            db.execute("INSERT INTO metadata VALUES ('linux_demo_inventory_v2', ?)",
                       (inventory_signature(db, event, key),))
        validate_rows(db, event, key_path, require_wal=True)


def demo_app(state_dir, config, origin, synthetic_config=None):
    validate_origin(origin)
    database = Path(state_dir) / DATABASE_NAME
    check_private(database.parent, directory=True)
    admin_key = staff_key_path(database)
    check_private(admin_key)
    if read_credentials(admin_key) is None:
        raise DemoError("DEMO 직원 계정의 비밀번호 검증 파일을 확인하세요.")
    return create_app(config, database, origin,
                      validate_existing=partial(validate_database, require_wal=True), synthetic_config=synthetic_config)


class JournalFormatter(logging.Formatter):
    """Framework errors may contain raw request URLs or header values."""
    def format(self, record):
        return f"{record.levelname} {record.name}"


def run_server(app, port):
    from flask.logging import default_handler
    from waitress import serve
    handler = logging.StreamHandler()
    handler.setFormatter(JournalFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    app.logger.removeHandler(default_handler)
    serve(app, host="127.0.0.1", port=port, threads=32, trusted_proxy="127.0.0.1",
          trusted_proxy_count=1, trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
          clear_untrusted_proxy_headers=True, max_request_body_size=4096,
          channel_timeout=30, connection_limit=256, expose_tracebacks=False)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Linux 비공개 DEMO 전용")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check", "serve", "migrate-contacts"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", required=True, type=Path)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--origin", required=True)
        if name != "prepare":
            command.add_argument("--synthetic-config", type=Path, required=name == "migrate-contacts")
        if name == "serve":
            command.add_argument("--port", type=int, default=8875)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.state_dir, args.config, args.origin)
            print("새 DEMO DB와 프로필 키를 준비했습니다. 직원 비밀번호를 별도로 설정하세요.")
        elif args.command == "migrate-contacts":
            migrate_contacts(args.state_dir, args.config, args.origin, args.synthetic_config)
            print("합성 연락처 이관 검증 완료")
        else:
            app = demo_app(args.state_dir, args.config, args.origin, args.synthetic_config)
            if args.command == "check":
                print("DEMO 기동 전 데이터·키·설정 검증 통과")
            elif 0 <= args.port <= 65535:
                run_server(app, args.port)
            else:
                parser.error("포트 범위를 확인하세요.")
    except DemoError as exc:
        parser.exit(1, f"DEMO 실패: {exc}\n")
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        # Config parsing failures may carry their source value.
        parser.exit(1, "DEMO 실패: 설정 및 DB·키 쌍, 파일 권한을 확인하세요.\n")


if __name__ == "__main__":
    main()
