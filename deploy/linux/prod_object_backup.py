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
import prod_backup_custody as custody
from prod_custody import CUTOFF, trusted_file
from prod_s3 import S3, UnexpectedSnapshot
import prod_local_lifecycle as lifecycle


DAY = 86400
RETENTION = 14 * DAY
FRESH = DAY + 2 * 3600


def canonical_json(record):
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def checked_clock(ledger, ctx):
    with ledger.transaction(*ctx) as (db, now):
        status = ledger._status(db)
    if status.retention_days != 14 or not status.backup_ready:
        raise ValueError("Approved fourteen-day inventory required")
    if status.closed and (now < CUTOFF or not status.purge_authorized):
        raise ValueError("Closed without authority for physical disposition")
    return now, status.closed


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


def export_lifecycle(store, ledger, work, prefix):
    decision = lifecycle.read_approval()
    raw = trusted_file(os.environ["MAISON_PROD_LIFECYCLE_APPROVAL"]).read_bytes()
    pin = os.environ["MAISON_PROD_LIFECYCLE_APPROVAL_SHA256"]
    backup.matching_digest(hashlib.sha256(raw).hexdigest(), pin)
    if json.loads(raw) != decision:
        raise ValueError("Approval changed during export")
    publish_bytes(store, "policy/" + prefix + pin + ".json", raw, work)
    record = lifecycle.evidence(ledger, decision)
    digest = hashlib.sha256(canonical_json(record)).hexdigest()
    publish_evidence(store, "lifecycle/" + prefix + str(record["observed_at"]) + "-" + digest + ".json", record, work)


@contextmanager
def journal(work, binding, ledger, ctx):
    """Exclude overlapping invocations. Each receipt is durable before the next side effect."""
    import fcntl
    fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = work / "delivery.sqlite"
        # Pin a delivery journal generation in the separately protected lifecycle state.
        # Losing the queue must never look like a first install with zero prior copies.
        with ledger.transaction(*ctx) as (control, _):
            control.execute("""CREATE TABLE IF NOT EXISTS delivery_anchor
                            (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, generation TEXT NOT NULL)""")
            anchor = control.execute("SELECT binding,generation FROM delivery_anchor").fetchone()
            if anchor and (anchor["binding"] != binding or not path.is_file()):
                raise ValueError("Missing or mismatched delivery journal; reconcile from independent evidence")
            if not path.exists():
                with backup.exclusive(path):
                    pass
            prod.private(path)
            with closing(sqlite3.connect(path)) as inner, inner:
                inner.execute("CREATE TABLE IF NOT EXISTS generation (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
                prior = inner.execute("SELECT value FROM generation").fetchone()
                if anchor and (not prior or prior[0] != anchor["generation"]):
                    raise ValueError("Restored delivery journal generation mismatch")
                if not anchor:
                    if inner.execute("SELECT 1 FROM sqlite_schema WHERE name='jobs'").fetchone():
                        raise ValueError("Unanchored delivery journal requires recovery review")
                    if not prior:
                        inner.execute("INSERT INTO generation VALUES(1,?)", (secrets.token_hex(32),))
                    generation = inner.execute("SELECT value FROM generation").fetchone()[0]
                    control.execute("INSERT INTO delivery_anchor VALUES(1,?,?)", (binding, generation))
            prod.sync_dir(work)
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


def daily(db, cfg, spool, slot, ledger, ctx, roots):
    if db.execute("SELECT 1 FROM jobs WHERE slot=?", (slot,)).fetchone():
        return
    # A crash after snapshot publication but before recording its digest leaves a
    # registered local-only archive. A retry creates a new name; POSIX expiry owns both.
    file = spool / (slot + "-" + secrets.token_hex(12) + ".cms")
    result = backup.create(Path(cfg["state_dir"]), Path(cfg["event"]), Path(cfg["recipient"]),
                           Path(cfg["certificate"]), cfg["cert_sha256"], file, authority=ledger)
    ident = roots.ident(file, "archive")
    row = next(r for r in ledger.objects(*ctx) if r["object_id"] == ident and r["deleted_at"] is None)
    db.execute("INSERT INTO jobs(name,slot,created,sha,bytes) VALUES (?,?,?,?,?)",
               (file.name, slot, row["created_at"], result["archive_sha256"], file.stat().st_size))


def retire(db, row, key, versions, ledger, ctx, store, work):
    # Persist the deletion inventory first. If a delete or receipt write fails,
    # retry against the same inventory and re-list, including noncurrent versions.
    planned = set(json.loads(row["deletion_plan"] or "[]"))
    planned.update(versions)
    db.execute("UPDATE jobs SET deletion_plan=? WHERE name=?", (json.dumps(sorted(planned)), row["name"]))
    for version in versions:
        store.delete(key, version)
    if any(k == key for k, _ in store.versions(store.data, key)):
        raise ValueError("Object versions still present after deletion")
    ident = external_id(store.data, key)
    evidence = dict(format="prod-object-expiry-v1", event_id=ctx[0], instance_id=ctx[1],
                    object_key=key, created_at=row["created"], archive_sha256=row["sha"],
                    versions=sorted(planned), verified_remaining=0, expires_at=min(row["created"] + RETENTION, CUTOFF))
    proof = publish_evidence(store, "expired/" + key + ".json", evidence, work)
    records = {r["object_id"]: r for r in ledger.objects(*ctx)}
    if ident in records:
        ledger.attest_deleted(*ctx, ident, proof)
    db.execute("UPDATE jobs SET erased=1 WHERE name=?", (row["name"],))


def deliver(db, row, key, versions, ledger, ctx, store, work, spool):
    if row["version"]:
        if versions != {row["version"]: "Versions"}:
            raise ValueError("Off-host backup missing or unexpected versions before expiry")
        return
    file = prod.private(spool / row["name"])
    with file.open("rb") as stream:
        backup.matching_digest(backup.digest_stream(stream), row["sha"])
    ledger.reserve(*ctx, "external", external_id(store.data, key))
    version = store.put(store.data, key, file, row["sha"])
    now, closed = checked_clock(ledger, ctx)
    if closed or now >= row["created"] + RETENTION:
        # A transfer started before the boundary may have crossed it.
        current = {v: kind for (k, v), kind in store.versions(store.data, key).items() if k == key}
        retire(db, row, key, current, ledger, ctx, store, work)
        return
    receipt = dict(format="prod-object-receipt-v1", event_id=ctx[0], instance_id=ctx[1], object_key=key,
                   created_at=row["created"], archive_sha256=row["sha"], version_id=version, size=row["bytes"])
    _, receipt_version = publish_bytes(store, "received/" + key + ".json", canonical_json(receipt), work)
    db.execute("UPDATE jobs SET version=?,receipt_version=? WHERE name=?", (version, receipt_version, row["name"]))


def run(cfg, ledger, store, *, health_only=False):
    event = prod.load_prod_event(cfg["event"])
    ctx = (event["id"], cfg["instance_id"], CUTOFF)
    now, closed = checked_clock(ledger, ctx)
    spool = prod.private(backup.detached(cfg["spool"]), directory=True)
    work = prod.private(backup.detached(cfg["work"]), directory=True)
    proofs = prod.private(backup.detached(cfg["posix_receipts"]), directory=True)
    roots = custody.Roots.from_env(*ctx[:2])
    if (spool not in roots.volumes or not {"archive", "temporary"} <= set(roots.volumes[spool]["kinds"])
            or "external" not in roots.manual or "snapshot" not in roots.manual
            or any(work.is_relative_to(v) or v.is_relative_to(work) for v in (*roots.volumes, proofs))):
        raise ValueError("Register isolated job state, spool and provider inventory")
    prefix = "prod-backups/" + ctx[0] + "/" + ctx[1] + "/"
    binding = canonical_json([*ctx, store.data, store.evidence, cfg["aws_account"], cfg["region"]]).decode()
    with journal(work, binding, ledger, ctx) as db:
        if not health_only:
            # Local storage expiry and creation do not wait on object storage or a Mac.
            custody.expire(roots, ledger, *ctx, proofs)
            if not closed:
                daily(db, cfg, spool, datetime.fromtimestamp(now, prod.KST).date().isoformat(), ledger, ctx, roots)
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
                ledger.flag_unknown(*ctx, external_id(store.data, key))
                if not health_only:
                    export_lifecycle(store, ledger, work, prefix)
                raise ValueError("Unknown or reappeared off-host archive; inventory review required")
        for key, row in jobs.items():
            if row["erased"]:
                continue
            versions = {v: kind for (k, v), kind in inventory.items() if k == key}
            current, closed = checked_clock(ledger, ctx)
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
                retire(db, row, key, versions, ledger, ctx, store, work)
            else:
                deliver(db, row, key, versions, ledger, ctx, store, work, spool)
        try:
            store.snapshots_clear()
        except UnexpectedSnapshot:
            ledger.flag_unknown(*ctx, external_id(cfg["aws_account"], cfg["region"] + "/" + cfg["lightsail_instance"]))
            if not health_only:
                export_lifecycle(store, ledger, work, prefix)
            raise
        if not health_only:
            export_lifecycle(store, ledger, work, prefix)
        with ledger.transaction(*ctx) as (control, _):
            if control.execute("SELECT 1 FROM unresolved LIMIT 1").fetchone():
                raise ValueError("Unresolved copy inventory requires custodian review")
        now, closed = checked_clock(ledger, ctx)
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
        raw = trusted_file(os.environ["MAISON_PROD_BACKUP_JOB"]).read_bytes()
        backup.matching_digest(hashlib.sha256(raw).hexdigest(), os.environ["MAISON_PROD_BACKUP_JOB_SHA256"])
        cfg = json.loads(raw)
        result = run(cfg, lifecycle.local_ledger(), S3(cfg), health_only=args.action == "health")
    except (OSError, RuntimeError, KeyError, ValueError, sqlite3.Error, subprocess.SubprocessError, backup.ops.OpsError):
        parser.exit(1, "Backup delivery or expiry is incomplete. Inspect private job, clock and inventory records.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
