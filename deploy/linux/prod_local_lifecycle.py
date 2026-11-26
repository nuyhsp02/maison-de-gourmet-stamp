"""Single-host PROD closure, locally pinned decisions and offline evidence export."""
import argparse
from contextlib import contextmanager
import ctypes
from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from prod_custody import (
    AuthorityError, CUTOFF, CustodyLedger, digest, trusted_file, validate_binding, validate_policy,
)


DEFAULT_LEDGER = "/var/lib/maison-prod-lifecycle/lifecycle.sqlite"
RETENTION_DAYS = 14
APPROVAL_OWNER_UID = 0


@lru_cache(maxsize=1)
def _query_clock():
    if not sys.platform.startswith("linux"):
        raise AuthorityError("The production clock check requires Linux")
    try:
        query = ctypes.CDLL(None, use_errno=True).adjtimex
    except (OSError, AttributeError):
        raise AuthorityError("Kernel clock verification unavailable") from None
    query.argtypes = [ctypes.c_void_p]
    query.restype = ctypes.c_int
    return query


def synchronized_time():
    # modes=0 queries the kernel NTP discipline locally. The zero-filled buffer exceeds
    # Linux struct timex on both 32/64-bit ABIs, uses native alignment, and is never interpreted.
    timex = (ctypes.c_long * 256)()
    if _query_clock()(ctypes.byref(timex)) not in (0, 1, 2, 3, 4):  # TIME_ERROR or syscall error
        raise AuthorityError("Kernel time synchronization unavailable")
    return time.time()


def read_approval():
    try:
        path = trusted_file(os.environ["MAISON_PROD_LIFECYCLE_APPROVAL"])
        pin = os.environ["MAISON_PROD_LIFECYCLE_APPROVAL_SHA256"]
        info = path.stat()
        if (info.st_uid != APPROVAL_OWNER_UID or path.parent.stat().st_uid != APPROVAL_OWNER_UID
                or not digest(pin) or info.st_size > 16384):
            raise ValueError
        data = path.read_bytes()
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), pin):
            raise ValueError
        record = json.loads(data)
        if (not isinstance(record, dict)
                or set(record) != {"format", "event_id", "instance_id", "access_until", "policy"}
                or record["format"] != "prod-lifecycle-approval-v1" or not digest(record["instance_id"])):
            raise ValueError
        validate_binding(record["event_id"], record["instance_id"], record["access_until"])
        validate_policy(record["policy"])
        if record["policy"]["retention_days"] != RETENTION_DAYS:
            raise ValueError
        return record
    except (KeyError, TypeError, OSError, ValueError):
        raise AuthorityError("Missing or invalid pinned production lifecycle approval") from None


def binding(record):
    return record["event_id"], record["instance_id"], record["access_until"]


class LocalLedger(CustodyLedger):
    """The local DB is mutable operational state. Its approval must match the operator's pinned copy."""
    def __init__(self, path, *, clock=None):
        super().__init__(path, clock=synchronized_time if clock is None else clock)

    @contextmanager
    def transaction(self, event_id, instance_id, cutoff, *, lookup=False):
        decision = read_approval()
        if (event_id != decision["event_id"] or cutoff != CUTOFF
                or instance_id not in (None, decision["instance_id"])):
            raise AuthorityError("Local lifecycle binding mismatch")
        with super().transaction(event_id, instance_id, cutoff, lookup=lookup) as (db, observed):
            row = db.execute("SELECT instance_id,policy,closed FROM event").fetchone()
            if (row["instance_id"] != decision["instance_id"]
                    or json.loads(row["policy"]) != decision["policy"]):
                raise AuthorityError("Stored custody or policy differs from approved binding")
            if not row["closed"] and int(self.clock()) < observed - 1:
                raise AuthorityError("Server clock moved behind recorded time")
            yield db, observed


def local_ledger():
    # Never initialize implicitly; a lost control directory during restore is an operator incident.
    try:
        return LocalLedger(os.environ.get("MAISON_PROD_LIFECYCLE", DEFAULT_LEDGER))
    except (OSError, ValueError):
        raise AuthorityError("Local production lifecycle state unavailable") from None


def evidence(ledger, decision):
    with ledger.transaction(*binding(decision)) as (db, observed):
        status = ledger._status(db)
        receipt = db.execute("SELECT live_receipt_at FROM event").fetchone()[0]
        remaining = db.execute("SELECT count(*) FROM objects WHERE deleted_at IS NULL").fetchone()[0]
        unknown = db.execute("SELECT count(*) FROM unresolved").fetchone()[0]
        return dict(format="prod-erasure-evidence-v1", event_id=decision["event_id"],
                    instance_id=status.instance_id, cutoff=CUTOFF, observed_at=observed,
                    closure=status.closed, local_db_purge_verified_at=receipt,
                    backups_expired=status.backups_expired, inventory_outstanding=remaining,
                    inventory_unresolved=unknown,
                    approval_sha256=os.environ["MAISON_PROD_LIFECYCLE_APPROVAL_SHA256"])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "approve", "evidence"])
    parser.add_argument("--ledger", type=Path, default=os.environ.get("MAISON_PROD_LIFECYCLE", DEFAULT_LEDGER))
    args = parser.parse_args()
    try:
        decision = read_approval()
        ctx = binding(decision)
        if args.action == "init":
            synchronized_time()
            CustodyLedger.create(args.ledger, *ctx, decision["policy"], clock=synchronized_time)
        elif args.action == "approve":
            # Only this explicit operator command can fill previously absent references.
            CustodyLedger(args.ledger, clock=synchronized_time).approve(*ctx, decision["policy"])
        print(json.dumps(evidence(LocalLedger(args.ledger), decision), sort_keys=True))
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Local lifecycle operation refused. Verify time, state and approved records.\n")


if __name__ == "__main__":
    main()
