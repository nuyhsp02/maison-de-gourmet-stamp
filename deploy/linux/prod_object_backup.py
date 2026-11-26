"""Create one daily encrypted PROD snapshot, deliver it, and reconcile expiry."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile

import prod
import prod_backup as backup
import prod_backup_source as data_source
from prod_custody import CUTOFF, validate_binding
from prod_s3 import S3, UnexpectedSnapshot


DAY = 86400
RETENTION = 14 * DAY
FRESH = DAY + 2 * 3600


def canonical_json(record):
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def destination_binding(cfg, ctx):
    return canonical_json([*ctx, cfg["data_bucket"], cfg["evidence_bucket"], cfg["aws_account"], cfg["region"]]).decode()


def external_id(bucket, key):
    return hashlib.sha256(("s3:" + bucket + "/" + key).encode()).hexdigest()


def publish_bytes(store, key, body, work):
    digest = hashlib.sha256(body).hexdigest()
    with tempfile.NamedTemporaryFile(dir=work) as tmp:
        tmp.write(body)
        tmp.flush()
        version = store.put(store.evidence, key, Path(tmp.name), digest)
    return digest, version


def publish_evidence(store, key, record, work):
    digest, _ = publish_bytes(store, key, canonical_json(record), work)
    return digest


def export_lifecycle(store, source, work, prefix, ctx):
    policy_text, record = source.evidence()
    raw = policy_text.encode()
    pin = os.environ["MAISON_PROD_LIFECYCLE_APPROVAL_SHA256"]
    backup.matching_digest(hashlib.sha256(raw).hexdigest(), pin)
    fixed = dict(format="prod-erasure-evidence-v1", event_id=ctx[0], instance_id=ctx[1],
                 approval_sha256=pin, cutoff=CUTOFF)
    counts = {"observed_at", "inventory_outstanding", "inventory_unresolved"}
    flags = {"closure", "backups_expired"}
    if (not isinstance(record, dict) or set(record) != fixed.keys() | counts | flags | {"local_db_purge_verified_at"}
            or any(record[k] != v for k, v in fixed.items())
            or any(type(record[k]) is not int or record[k] < 0 for k in counts)
            or any(type(record[k]) is not bool for k in flags)
            or record["local_db_purge_verified_at"] is not None and type(record["local_db_purge_verified_at"]) is not int):
        raise ValueError("Evidence does not match the approved source")
    publish_bytes(store, "policy/" + prefix + pin + ".json", raw, work)
    digest = hashlib.sha256(canonical_json(record)).hexdigest()
    publish_evidence(store, "lifecycle/" + prefix + str(record["observed_at"]) + "-" + digest + ".json", record, work)


@contextmanager
def journal(work, binding, source):
    """Exclude overlapping invocations. Each receipt is durable before the next side effect."""
    import fcntl
    fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = work / "delivery.sqlite"
        # Pin a delivery journal generation in the separately protected lifecycle state.
        # Losing the queue must never look like a first install with zero prior copies.
        anchor = source.anchor()
        if anchor and not path.is_file():
            raise ValueError("Missing or mismatched delivery journal; reconcile from independent evidence")
        if not path.exists():
            with backup.exclusive(path):
                pass
        prod.private(path)
        with closing(sqlite3.connect(path)) as inner, inner:
            inner.execute("CREATE TABLE IF NOT EXISTS generation (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            prior = inner.execute("SELECT value FROM generation WHERE id=1").fetchone()
            if anchor and (not prior or prior[0] != anchor):
                raise ValueError("Restored delivery journal generation mismatch")
            if not anchor and inner.execute("SELECT 1 FROM sqlite_schema WHERE name='jobs'").fetchone():
                raise ValueError("Unanchored delivery journal requires recovery review")
            if not prior:
                inner.execute("INSERT INTO generation VALUES(1,?)", (secrets.token_hex(32),))
            generation = inner.execute("SELECT value FROM generation WHERE id=1").fetchone()[0]
        prod.sync_dir(work)
        if source.anchor(generation) != generation:
            raise ValueError("Source journal binding mismatch")
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO binding VALUES (1,?)", (binding,))
            if db.execute("SELECT value FROM binding").fetchone()[0] != binding:
                raise ValueError("Delivery journal belongs to another source or destination")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                name TEXT PRIMARY KEY, slot TEXT NOT NULL, created INTEGER NOT NULL,
                sha TEXT NOT NULL, bytes INTEGER NOT NULL, version TEXT, receipt_version TEXT, deletion_plan TEXT, erased INTEGER
            )""")
            yield db
    finally:
        os.close(fd)


def daily(db, slot, source):
    if db.execute("SELECT 1 FROM jobs WHERE slot=?", (slot,)).fetchone():
        return
    # The isolated transfer worker chooses the archive name and times the snapshot.
    name = slot + "-" + secrets.token_hex(12) + ".cms"
    before, _ = source.clock()
    created, sha, size = source.snapshot(name)
    after, closed = source.clock()
    if closed or type(created) is not int or not before <= created <= after or type(size) is not int or size <= 0:
        raise ValueError("Unconfirmed snapshot creation time or size")
    backup.matching_digest(sha, sha)
    db.execute("INSERT INTO jobs(name,slot,created,sha,bytes) VALUES (?,?,?,?,?)", (name, slot, created, sha, size))


def retire(db, row, key, versions, source, ctx, store, work):
    # Persist the deletion inventory first. If a delete or receipt write fails,
    # retry against the same inventory and re-list, including noncurrent versions.
    planned = set(json.loads(row["deletion_plan"] or "[]"))
    planned.update(versions)
    db.execute("UPDATE jobs SET deletion_plan=? WHERE name=?", (json.dumps(sorted(planned)), row["name"]))
    for version in versions:
        store.delete(key, version)
    if any(k == key for k, _ in store.versions(store.data, key)):
        raise ValueError("Object versions still present after deletion")
    evidence = dict(format="prod-object-expiry-v1", event_id=ctx[0], instance_id=ctx[1],
                    object_key=key, created_at=row["created"], archive_sha256=row["sha"],
                    versions=sorted(planned), verified_remaining=0, expires_at=min(row["created"] + RETENTION, CUTOFF))
    proof = publish_evidence(store, "expired/" + key + ".json", evidence, work)
    source.retired(row["name"], proof)
    db.execute("UPDATE jobs SET erased=1 WHERE name=?", (row["name"],))


def deliver(db, row, key, versions, source, ctx, store, work):
    if row["version"]:
        if versions != {row["version"]: "Versions"}:
            raise ValueError("Off-host backup missing or unexpected versions before expiry")
        return
    # Copy the checked read-only CMS descriptor into an anonymous, worker-owned file.
    # The AWS CLI may reopen its procfd as maison-backup. No crash leaves a named copy.
    with os.fdopen(source.archive(row["name"]), "rb") as stream, tempfile.TemporaryFile(dir=work) as ciphertext:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if ciphertext.tell() + len(chunk) > row["bytes"]:
                raise ValueError("Source archive exceeds its recorded size")
            digest.update(chunk)
            ciphertext.write(chunk)
        backup.matching_digest(digest.hexdigest(), row["sha"])
        if ciphertext.tell() != row["bytes"]:
            raise ValueError("Source archive size mismatch")
        ciphertext.flush()
        ciphertext.seek(0)
        source.reserve(row["name"])
        # Recheck expiry after obtaining/copying the file and immediately before upload.
        now, closed = source.clock()
        if closed or now >= row["created"] + RETENTION:
            retire(db, row, key, versions, source, ctx, store, work)
            return
        fdroot = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
        version = store.put(store.data, key, fdroot / str(ciphertext.fileno()), row["sha"])
    now, closed = source.clock()
    if closed or now >= row["created"] + RETENTION:
        # A transfer started before the boundary may have crossed it.
        current = {v: kind for (k, v), kind in store.versions(store.data, key).items() if k == key}
        retire(db, row, key, current, source, ctx, store, work)
        return
    receipt = dict(format="prod-object-receipt-v1", event_id=ctx[0], instance_id=ctx[1], object_key=key,
                   created_at=row["created"], archive_sha256=row["sha"], version_id=version, size=row["bytes"])
    _, receipt_version = publish_bytes(store, "received/" + key + ".json", canonical_json(receipt), work)
    db.execute("UPDATE jobs SET version=?,receipt_version=? WHERE name=?", (version, receipt_version, row["name"]))


def run(cfg, ledger, store, *, health_only=False):
    # In-process source is used by existing isolated drills. The installed CLI always
    # connects as the dedicated transfer UID to the socket-activated local data owner.
    ctx = data_source.binding(cfg)
    plan = dict(cfg, data_bucket=store.data, evidence_bucket=store.evidence)
    source = data_source.SnapshotSource(plan, ledger, destination_binding(plan, ctx))
    return transfer(plan, ctx, source, store, health_only=health_only)


def transfer(cfg, ctx, source, store, *, health_only=False):
    now, closed = source.clock()
    work = prod.private(backup.detached(cfg["work"]), directory=True)
    prefix = "prod-backups/" + ctx[0] + "/" + ctx[1] + "/"
    with journal(work, destination_binding(cfg, ctx), source) as db:
        if not health_only:
            source.expire()
            if not closed:
                daily(db, datetime.fromtimestamp(now, prod.KST).date().isoformat(), source)
        store.ready()
        store.uploads_clear(prefix)
        inventory = store.versions(store.data, prefix)
        receipts = store.versions(store.evidence, "received/" + prefix)
        jobs = {prefix + r["name"]: r for r in db.execute("SELECT * FROM jobs ORDER BY created")}
        expected_receipts = {"received/" + key + ".json" for key in jobs}
        if any(key not in expected_receipts for key, version in receipts):
            raise ValueError("Independent receipts exceed local delivery history; reconcile before continuing")
        for key, _ in inventory:
            if key not in jobs or jobs[key]["erased"]:
                source.unknown(external_id(store.data, key))
                if not health_only:
                    export_lifecycle(store, source, work, prefix, ctx)
                raise ValueError("Unknown or reappeared off-host archive; inventory review required")
        for key, row in jobs.items():
            if row["erased"]:
                continue
            versions = {v: kind for (k, v), kind in inventory.items() if k == key}
            current, closed = source.clock()
            if row["version"] and not closed and current < row["created"] + RETENTION:
                receipt_key = "received/" + key + ".json"
                available = {v: kind for (k, v), kind in receipts.items() if k == receipt_key}
                if available != {row["receipt_version"]: "Versions"}:
                    raise ValueError("Independent delivery receipt missing or superseded")
            if health_only:
                if (not closed and current < row["created"] + RETENTION
                        and versions != {row["version"]: "Versions"}):
                    raise ValueError("Expected a verified off-host backup")
                if (closed or current >= row["created"] + RETENTION) and versions:
                    raise ValueError("Expired versions remain in storage")
            elif closed or current >= row["created"] + RETENTION:
                retire(db, row, key, versions, source, ctx, store, work)
            else:
                deliver(db, row, key, versions, source, ctx, store, work)
        try:
            store.snapshots_clear()
        except UnexpectedSnapshot:
            source.unknown(external_id(cfg["aws_account"], cfg["region"] + "/" + cfg["lightsail_instance"]))
            if not health_only:
                export_lifecycle(store, source, work, prefix, ctx)
            raise
        if not health_only:
            export_lifecycle(store, source, work, prefix, ctx)
        source.clear()
        now, closed = source.clock()
        if closed:
            if store.versions(store.data, prefix):
                raise ValueError("Cutoff reached with pending objects")
            if not health_only:
                publish_evidence(store, "reconciled/" + prefix + "empty.json",
                                 dict(format="prod-object-cutoff-v1", event_id=ctx[0], instance_id=ctx[1],
                                      cutoff=CUTOFF, version_count=0, regional_instance_snapshot_count=0), work)
        else:
            last = db.execute("SELECT max(created) FROM jobs WHERE version IS NOT NULL AND erased IS NULL").fetchone()[0]
            if last is None or not 0 <= now - last < FRESH:
                raise ValueError("Latest off-host receipt is missing or stale")
        if not health_only:
            store.heartbeat(ctx[0])
        return {"storage_verified": True, "closed": closed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "health"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        cfg = data_source.configuration()
        ctx = (cfg["event_id"], cfg["instance_id"], CUTOFF)
        validate_binding(*ctx)
        with data_source.connect() as source:
            result = transfer(cfg, ctx, source, S3(cfg), health_only=args.action == "health")
    except (OSError, RuntimeError, KeyError, ValueError, sqlite3.Error, subprocess.SubprocessError, backup.ops.OpsError):
        parser.exit(1, "Backup delivery or expiry is incomplete. Inspect private job, clock and inventory records.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
