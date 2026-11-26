"""Erase one already-closed PROD instance. Publishing live evidence belongs to the caller."""
from contextlib import closing
import os
from pathlib import Path
import re
import time

import prod


# Children precede their parents. Even an unavailable contact lookup can carry a customer ID.
CUSTOMER_TABLES = (
    "contact_access_audit", "stamp_correction_audit", "stamp_cancel_audit", "reward_audit",
    "sessions", "admin_sessions", "customer_contacts", "stamps", "customer_profiles", "customers",
)
MARKER = "prod_privacy_closed_v1"
METADATA_KEYS = {
    "event", "linux_prod_v1", "prod_instance_id", "profile_key_check_v1", "prod_inventory_v1",
    "admin_schema_v1", "contact_recipient_v1", "correction_schema_v1",
    prod.staff_admin.PROD_STAFF_BINDING, prod.staff_admin.MANAGER_BINDING, MARKER,
}


def has_local_purge(db):
    return db.execute("SELECT 1 FROM metadata WHERE key=?", (MARKER,)).fetchone() is not None


def check_instance_files(database):
    prod.check_files(database)
    allowed = {database.name + suffix for suffix in (
        "", "-wal", "-shm", "-journal", ".profile.key", ".admin.key", ".manager.key",
    )}
    for file in database.parent.iterdir():
        if file.name not in allowed:
            raise prod.ProdError("Unclassified instance file; review retention inventory")
        prod.private(file)
        # An abandoned rollback journal can contain old database pages. Do not
        # silently discard it, or certify that a WAL checkpoint erased it.
        if file.name == database.name + "-journal" and file.stat().st_size:
            raise prod.ProdError("Unclassified rollback journal; reconcile the private inventory")


def erase_rows(db, cutoff):
    meta = dict(db.execute("SELECT key,value FROM metadata"))
    if meta.keys() - METADATA_KEYS or (MARKER in meta and meta[MARKER] != str(cutoff)):
        raise prod.ProdError("Unclassified production metadata; review disposition")
    for binding in (prod.staff_admin.PROD_STAFF_BINDING, prod.staff_admin.MANAGER_BINDING):
        if binding in meta and not re.fullmatch(r"(?:[0-9a-f]{64}|revoked)", meta[binding]):
            raise prod.ProdError("Unclassified credential metadata")
    # ANALYZE may have saved actual customer index keys in sqlite_stat4.sample.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    planner_tables = tables.intersection({"sqlite_stat1", "sqlite_stat4"})
    if tables - planner_tables != set(CUSTOMER_TABLES) | {"products", "metadata"}:
        raise prod.ProdError("Unclassified production table; review disposition")
    # prod.validate_rows matched each trigger against the shipped immutable schema.
    # Only this authorized transaction temporarily suspends redeemed/correction deletion guards.
    guards = ("protect_correction_delete", "protect_redeemed_stamp")
    originals = [db.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
                 for name in guards]
    for name in guards:
        db.execute("DROP TRIGGER " + name)
    for table in (*CUSTOMER_TABLES, *sorted(planner_tables)):
        db.execute("DELETE FROM " + table)
        if db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone():
            raise prod.ProdError("Incomplete customer erasure")
    for sql in originals:
        db.execute(sql)
    db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (MARKER, str(cutoff)))


def compact(db):
    # A failed physical step leaves the local closure committed. Retry the same operation.
    db.execute("VACUUM")
    if db.execute("PRAGMA freelist_count").fetchone()[0]:
        raise prod.ProdError("Production database still contains free pages")
    checkpoint = tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    if checkpoint != (0, 0, 0):
        raise prod.ProdError("Production WAL could not be fully truncated")


def purge(state_dir, event, recipient, authority):
    """Return local evidence only after a successful commit, compaction, checkpoint and fsync."""
    import fcntl
    if event.get("environment") != "production" or not event["id"].startswith("prod-"):
        raise prod.ProdError("A confirmed production event is required")
    cutoff = prod.application.timestamp(event["access_until"])
    database = prod.database_path(state_dir)
    check_instance_files(database)
    key_path = prod.application.default_profile_key_path(database)
    fd = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(prod.application.connect_db(database, mode="rw")) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA temp_store=MEMORY")
            if db.execute("PRAGMA secure_delete=ON").fetchone()[0] != 1:
                raise prod.ProdError("SQLite secure deletion is required")
            # Acquire a writer lock before identity, disposition approval, and eligibility checks.
            # In WAL mode the final TRUNCATE must also drain any pre-cutoff reader's old snapshot.
            with db:
                db.execute("BEGIN IMMEDIATE")
                prod.validate_rows(db, event, key_path, recipient)
                instance = prod.staff_admin.prod_instance(db)
                query = authority.close if time.time() >= cutoff else authority.status
                status = query(event["id"], instance, cutoff)
                if status.instance_id != instance or not status.closed or not status.purge_authorized:
                    raise prod.ProdError("Destruction needs closure, approved scope and verified backup recovery")
                erase_rows(db, cutoff)
                prod.validate_rows(db, event, key_path, recipient)
            compact(db)
        check_instance_files(database)
        wal = Path(str(database) + "-wal")
        if wal.exists() and wal.stat().st_size != 0:
            raise prod.ProdError("Production WAL is not empty")
        with database.open("rb") as stream:
            os.fsync(stream.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"format": "prod-local-erasure-v1", "event_id": event["id"], "instance_id": instance,
            "access_until": cutoff, "customer_tables_empty": True, "wal_truncated": True}
