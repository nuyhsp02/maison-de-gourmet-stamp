"""Linux entry point for an independently prepared production event."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache, partial
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit

APP_DIR = Path(__file__).resolve().parents[2] / "app"
sys.path.insert(0, str(APP_DIR))
import app as application
import contact_privacy as contact
import staff_admin

DATABASE_NAME = "prod.sqlite"
DEMO_STATE = Path("/var/lib/maison-demo")
KST = timezone(timedelta(hours=9))


class ProdError(ValueError):
    """Keep configuration, credential and customer values out of diagnostics."""


def canonical(path, *, new=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ProdError("Absolute canonical path required")
    if path.is_relative_to(DEMO_STATE) or DEMO_STATE.is_relative_to(path):
        raise ProdError("DEMO path refused")
    existing = path.parent if new else path
    if existing.resolve(strict=True) != existing:
        raise ProdError("Symbolic or noncanonical path refused")
    return path


def private(path, *, directory=False):
    path = canonical(path)
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (not kind(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode
            or (not directory and info.st_nlink != 1)):
        raise ProdError("Expected an owner-only private directory or a single private regular file")
    return path


def database_path(state_dir):
    return private(state_dir, directory=True) / "instance" / DATABASE_NAME


def separate_destination(path):
    # Also recognize service layouts in custom locations. Never open their databases.
    if path.is_relative_to(APP_DIR.parent):
        raise ProdError("Use a directory outside the application package")
    for parent in (path, *path.parents):
        if any((parent / name).exists() for name in (".git", "collection.sqlite", DATABASE_NAME,
                                                      "instance/" + DATABASE_NAME)):
            raise ProdError("Use a directory outside Git worktrees and existing service state")


def validate_origin(origin):
    url = urlsplit(origin)
    host = url.hostname or ""
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (origin != f"https://{host}" or not re.fullmatch(rf"{label}(?:\.{label})+", host)
            or host.rsplit(".", 1)[-1].isdigit() or len(host) > 253
            or any(part == "demo" or part.startswith("demo-") for part in host.split("."))):
        raise ProdError("A dedicated HTTPS production origin is required")


def load_prod_event(config):
    config = canonical(config)
    try:
        event = json.loads(config.read_text(encoding="utf-8"))
        if (not isinstance(event, dict) or event.get("environment") != "production"
                or not re.fullmatch(r"prod-[a-z0-9][a-z0-9-]{0,58}", event.get("id", ""))
                or event.get("draft") is not False):
            raise ProdError("Production event and confirmed catalogue required")
        application.validate_event(event)
        validate_origin(event["public_origin"])
        fields = {"environment", "id", "draft", "public_origin", "cafe_name", "title", "description",
                  "reward", "starts_at", "ends_at", "reward_until", "access_until", "products"}
        if set(event) != fields or not all(isinstance(event[k], str) and event[k].strip()
                                           for k in ("cafe_name", "title", "description", "reward")):
            raise ProdError("Incomplete production event")
        for product in event["products"]:
            if (set(product) != {"id", "name", "short_name", "category", "description", "qr_product_id", "image"}
                    or product["qr_product_id"] != product["id"] or product["image"] is not None
                    or not all(isinstance(product[k], str) and product[k].strip()
                               for k in ("name", "short_name", "description"))):
                raise ProdError("Each product needs approved text, explicit QR mapping and image selection")
        dates = [datetime.fromisoformat(event[k].replace("Z", "+00:00")) for k in
                 ("starts_at", "ends_at", "reward_until", "access_until")]
        if (any(dt.utcoffset() is None for dt in dates)
                or dates[0].astimezone(KST).date() != datetime(2026, 12, 1).date()
                or dates[1:] != [datetime(2027, month, day, tzinfo=KST) for month, day in ((2, 1), (3, 1), (3, 31))]):
            raise ProdError("Production event boundaries differ from the confirmed schedule")
        return event
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProdError("Invalid production event, catalogue or schedule") from None


def create_schema(db):
    # Only ever used for a new, unpublished DB or an in-memory reference.
    for source in ("schema.sql", "contact_schema.sql", "correction_schema.sql"):
        db.executescript((APP_DIR / source).read_text(encoding="utf-8"))
    db.execute("DROP TABLE recovery")


@lru_cache(maxsize=1)
def expected_schema():
    with closing(sqlite3.connect(":memory:")) as db:
        create_schema(db)
        return contact.schema_rows(db)


def inventory(db, event, key, recipient_id, instance_id):
    rows = [tuple(row) for row in db.execute("SELECT id,qr_key FROM products ORDER BY id")]
    if ([pid for pid, _ in rows] != sorted(p["id"] for p in event["products"])
            or len({qr for _, qr in rows}) != 10 or any(not isinstance(qr, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32}", qr) for _, qr in rows)):
        raise ProdError("Product or QR inventory mismatch")
    payload = json.dumps([event, instance_id, recipient_id, rows], sort_keys=True, separators=(",", ":")).encode()
    return hmac.digest(key, b"maison/prod/inventory/v1\0" + payload, "sha256").hex()


def validate_rows(db, event, key_path, recipient, *, require_wal=True):
    meta = dict(db.execute("SELECT key,value FROM metadata"))
    if (meta.get("linux_prod_v1") != "1" or any(k.startswith("linux_demo_") for k in meta)
            or meta.get("event") != application.event_signature(event) or meta.get("admin_schema_v1") != "1"
            or meta.get("correction_schema_v1") != "1" or not re.fullmatch(r"[0-9a-f]{64}", meta.get("prod_instance_id", ""))
            or not isinstance(recipient, contact.ProductionEnrollment) or recipient.event_id != event["id"]
            or meta.get("contact_recipient_v1") != recipient.key_id or contact.schema_rows(db) != expected_schema()):
        raise ProdError("Production instance, event, schema or contact recipient mismatch")
    key = application.verify_profile_key(db, private(key_path))
    if key is None or not hmac.compare_digest(meta.get("prod_inventory_v1", ""),
                                               inventory(db, event, key, recipient.key_id, meta["prod_instance_id"])):
        raise ProdError("Production inventory or profile key mismatch")
    if require_wal and db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        raise ProdError("WAL mode required")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ProdError("Database integrity check failed")
    return key


def check_files(database):
    private(database.parent, directory=True)
    private(database)
    private(application.default_profile_key_path(database))
    for suffix in ("-shm", "-wal", "-journal"):
        path = Path(str(database) + suffix)
        if path.exists() or path.is_symlink():
            private(path)


def check_roles(db, database):
    for path in (staff_admin.staff_key_path(database), staff_admin.manager_key_path(database)):
        private(path)
    staff_admin.validate_prod_credentials(db, database)


def validate_database(database, event, recipient, *, require_wal=True, credentials=False):
    database = Path(database)
    check_files(database)
    with closing(application.connect_db(database, mode="ro")) as db:
        db.execute("BEGIN")
        key = validate_rows(db, event, application.default_profile_key_path(database), recipient, require_wal=require_wal)
        if credentials:
            check_roles(db, database)
        return key


def require_origin(event, origin):
    validate_origin(origin)
    if origin != event["public_origin"]:
        raise ProdError("Origin differs from the production event")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(state_dir, config, origin, recipient):
    import fcntl
    event = load_prod_event(config)
    require_origin(event, origin)
    if not isinstance(recipient, contact.ProductionEnrollment) or event["id"] != recipient.event_id:
        raise ProdError("Wrong production contact recipient")
    state = private(state_dir, directory=True)
    separate_destination(state)
    directory_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(state.iterdir()):
            raise ProdError("Preparation requires a new empty production state directory")
        with tempfile.TemporaryDirectory(dir=state, prefix=".prepare-") as tmp:
            stage = Path(tmp) / "instance"
            stage.mkdir(mode=0o700)
            database = stage / DATABASE_NAME
            database.touch(mode=0o600, exist_ok=False)
            with closing(application.connect_db(database, mode="rw")) as db:
                create_schema(db)
                db.execute("BEGIN IMMEDIATE")
                with db:
                    key = application.bind_profile_key(db, application.default_profile_key_path(database))
                    instance_id = secrets.token_hex(32)
                    db.executemany("INSERT INTO products VALUES (?,?)",
                                   [(p["id"], secrets.token_urlsafe(24)) for p in event["products"]])
                    db.executemany("INSERT INTO metadata VALUES (?,?)", [
                        ("linux_prod_v1", "1"), ("prod_instance_id", instance_id),
                        ("event", application.event_signature(event)), ("admin_schema_v1", "1"),
                        ("contact_recipient_v1", recipient.key_id), ("correction_schema_v1", "1"),
                        ("prod_inventory_v1", inventory(db, event, key, recipient.key_id, instance_id)),
                    ])
                db.execute("PRAGMA journal_mode=WAL")
            validate_database(database, event, recipient)
            sync_dir(stage)
            if set(state.iterdir()) != {Path(tmp)}:
                raise ProdError("Production target is no longer empty")
            stage.rename(state / "instance")
            sync_dir(state)
    finally:
        os.close(directory_fd)
    return database_path(state)


class PrivacyAccess:
    """Bind live custody attestations to this exact event and prepared instance."""
    def __init__(self, state_dir, event, recipient, authority=None):
        from prod_authority import Authority
        state = private(state_dir, directory=True)
        database = database_path(state)
        check_files(database)
        with closing(application.connect_db(database, mode="ro")) as db:
            # Verify the target before writing even a denial latch or notifying the external host.
            db.execute("BEGIN")
            validate_rows(db, event, application.default_profile_key_path(database), recipient)
            instance = staff_admin.prod_instance(db)
            db_was_erased = db.execute("SELECT 1 FROM metadata WHERE key='prod_privacy_closed_v1'").fetchone()
        self.binding = (event["id"], instance, application.timestamp(event["access_until"]))
        self.authority = authority if authority is not None else Authority.from_env()
        self.latch = state / ".privacy-closed"
        if db_was_erased:
            self.latch_closed()

    def latch_closed(self):
        # Separate from instance/ so replacing the DB cannot undo an observed cutoff.
        try:
            self.latch.mkdir(mode=0o700)
        except FileExistsError:
            self.verify_latch()
        sync_dir(self.latch.parent)

    def verify_latch(self):
        if any(private(self.latch, directory=True).iterdir()):
            raise ProdError("Unexpected material in production closure latch")

    def status(self):
        cutoff = self.binding[2]
        locally_closed = self.latch.is_symlink() or self.latch.exists()
        if locally_closed:
            self.verify_latch()
        if time.time() >= cutoff and not locally_closed:
            self.latch_closed()
            locally_closed = True
        status = self.authority.status(*self.binding)
        if status.instance_id != self.binding[1]:
            raise ProdError("Custody identity mismatch")
        if status.closed:
            if not locally_closed:
                self.latch_closed()
        elif locally_closed or time.time() >= cutoff:
            self.latch_closed()
            self.authority.close(*self.binding)
            status = self.authority.status(*self.binding)
            if not status.closed or status.instance_id != self.binding[1]:
                raise ProdError("Irreversible closure not confirmed")
        return status

    def access(self):
        # The web and SSH handlers never use a cached affirmative decision.
        try:
            status = self.status()
            if status.closed:
                return "closed"
            if status.ready and time.time() < self.binding[2]:
                return "open"
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            pass
        return "unavailable"


def run_purge(state_dir, event, recipient, access):
    import fcntl
    from prod_purge import purge
    live_root = private(os.environ.get("MAISON_PROD_LIVE_STATE", "/var/lib/maison-prod"), directory=True)
    if private(state_dir, directory=True) != live_root:
        raise ProdError("Live erasure receipt requires the configured active service root")
    if not access.status().closed:
        raise ProdError("Privacy deadline has not been reached")
    fd = os.open(live_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Backup holds a shared lock on this root throughout snapshot and encryption.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        access.verify_latch()
        if {p.name for p in live_root.iterdir()} != {"instance", ".privacy-closed"}:
            raise ProdError("Unexpected copy or staging file in the active state root")
        purge(live_root, event, recipient, access.authority)
        access.authority.record_purge(*access.binding)
    finally:
        os.close(fd)


def check(state_dir, config, origin, recipient, *, authority=None):
    event = load_prod_event(config)
    require_origin(event, origin)
    status = PrivacyAccess(state_dir, event, recipient, authority).status()
    if not status.ready and not status.closed:
        raise ProdError("Independent retention policy and custody checks required")
    database = database_path(state_dir)
    validate_database(database, event, recipient, credentials=not status.closed)
    if status.closed:
        from prod_purge import CUSTOMER_TABLES, has_local_purge
        with closing(application.connect_db(database, mode="ro")) as db:
            db.execute("BEGIN")
            if (not status.purged or not status.backups_expired or not has_local_purge(db)
                    or any(db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                           for table in CUSTOMER_TABLES)):
                raise ProdError("Live erasure or complete backup expiry has not been verified")


def prod_app(state_dir, config, origin, recipient, *, authority=None):
    event = load_prod_event(config)
    require_origin(event, origin)
    access = PrivacyAccess(state_dir, event, recipient, authority)
    status = access.status()
    if status.closed:
        # Booting an older DB, even after a recorded purge, must erase it again first.
        run_purge(state_dir, event, recipient, access)
    elif not status.ready:
        raise ProdError("Independent retention policy and custody checks required")
    return application.create_app(config, database_path(state_dir), origin, production_event=event,
                                  production_enrollment=recipient, privacy_access=access.access,
                                  validate_existing=partial(validate_database, recipient=recipient,
                                                            credentials=not status.closed))


def export_qr(state_dir, config, origin, recipient, output):
    from qrcode import QRCode
    from qrcode.image.svg import SvgPathImage
    event = load_prod_event(config)
    require_origin(event, origin)
    database = database_path(state_dir)
    check_files(database)
    with closing(application.connect_db(database, mode="ro")) as db:
        db.execute("BEGIN")
        validate_rows(db, event, application.default_profile_key_path(database), recipient)
        check_roles(db, database)
        keys = dict(db.execute("SELECT id,qr_key FROM products"))
    output = canonical(output, new=True)
    private(output.parent, directory=True)
    separate_destination(output)
    if output.is_relative_to(state_dir) or Path(state_dir).is_relative_to(output):
        raise ProdError("Use a separate, private QR output directory")
    output.mkdir(mode=0o700)
    try:
        # Numbered filenames avoid collisions between customer-provided product IDs and the entry card.
        cards = [dict(kind="entry", file="entry.svg", url=origin + "/")]
        for number, p in enumerate(event["products"], 1):
            cards.append(dict(kind="product", product_id=p["id"], name=p["name"],
                              file=f"product-{number:02}.svg", url=origin + "/q/" + keys[p["id"]]))
        for card in cards:
            qr = QRCode(border=4)
            qr.add_data(card["url"])
            qr.make(fit=True)
            with open(output / card["file"], "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
                qr.make_image(image_factory=SvgPathImage).save(f)
        with open(output / "manifest.json", "x", encoding="utf-8",
                  opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
            json.dump(cards, f, ensure_ascii=False)
    except BaseException:
        shutil.rmtree(output)
        raise


class PrivateFormatter(logging.Formatter):
    def format(self, record):
        return f"{record.levelname} {record.name}"


def serve_app(app, port):
    from flask.logging import default_handler
    from waitress import serve
    handler = logging.StreamHandler()
    handler.setFormatter(PrivateFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    app.logger.removeHandler(default_handler)
    serve(app, host="127.0.0.1", port=port, threads=32, trusted_proxy="127.0.0.1",
          trusted_proxy_count=1, trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
          clear_untrusted_proxy_headers=True, max_request_body_size=4096,
          channel_timeout=30, connection_limit=256, expose_tracebacks=False)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Linux PROD event operations")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check", "serve", "qr-export", "purge"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--origin", required=True)
        command.add_argument("--recipient", type=Path, required=True)
        if name == "purge":
            command.add_argument("--event-id", required=True)
        if name == "serve":
            command.add_argument("--port", type=int, default=8876)
            command.add_argument("--policy", type=Path)
            command.add_argument("--approved-policy-sha256")
        if name == "qr-export":
            command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = load_prod_event(args.config)
        recipient = contact.ProductionEnrollment.read(canonical(args.recipient), event["id"],
                         getattr(args, "policy", None), approved_policy_sha256=getattr(args, "approved_policy_sha256", None))
        if args.command == "prepare":
            prepare(args.state_dir, args.config, args.origin, recipient)
        elif args.command == "purge":
            require_origin(event, args.origin)
            if args.event_id != event["id"]:
                raise ProdError("Wrong event for erasure")
            run_purge(args.state_dir, event, recipient, PrivacyAccess(args.state_dir, event, recipient))
        elif args.command == "check":
            check(args.state_dir, args.config, args.origin, recipient)
        elif args.command == "qr-export":
            export_qr(args.state_dir, args.config, args.origin, recipient, args.out)
        else:
            serve_app(prod_app(args.state_dir, args.config, args.origin, recipient), args.port)
    except (OSError, ValueError, sqlite3.Error, TypeError):
        parser.exit(1, "PROD operation failed. Check the isolated state, event, recipients and credentials.\n")
    print("PROD operation completed.")


if __name__ == "__main__":
    main()
