"""Offline authorization to decrypt one PROD archive into one isolated destination."""
from contextlib import closing
from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from prod_custody import CUTOFF, CustodyLedger, digest, trusted_file, validate_binding, validate_policy


# Installed by the recovery administrator, separately from restore media. No CLI
# switch or environment setting can replace the trust anchor with a caller's key.
APPROVER_KEY = Path("/etc/maison-restore/approver.der")
TRUST_OWNER = 0
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
MAX_AGE = 3600
DAY = 86400


def require(ok):
    if not ok:
        raise ValueError("Offline restore decision, evidence, recipient or validity window mismatch")


def read_small(path, limit=16384):
    if path is None:
        raise ValueError("Independent restore approval, signature and evidence are required")
    path = trusted_file(path)
    require(path.stat().st_size <= limit)
    return path.read_bytes()


def verification_key():
    # /etc itself may resolve to /private/etc on macOS.
    parent = APPROVER_KEY.parent.parent.resolve(strict=True)
    path = parent / APPROVER_KEY.parent.name / APPROVER_KEY.name
    for node in (path, path.parent, parent):
        st = node.lstat()
        require(st.st_uid == TRUST_OWNER and not st.st_mode & 0o022)
        if node == path:
            require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
        else:
            require(stat.S_ISDIR(st.st_mode))
    der = read_small(path, 44)
    require(len(der) == 44 and der.startswith(ED25519_SPKI_PREFIX))
    return VerifyKey(der[len(ED25519_SPKI_PREFIX):])


def sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class VerifiedDecision:
    binding: tuple
    closed: bool
    request_id: str
    decision_sha256: str
    register_id: str
    created_at: int


@dataclass(frozen=True)
class ApprovalFiles:
    decision: Path
    signature: Path
    receipt: Path
    lifecycle: Path
    policy: Path

    def verify(self, now, event, config_digest, archive, archive_digest, cert_digest, destination, roots_factory, *, initial=False):
        decision = read_small(self.decision)
        try:
            verification_key().verify(decision, read_small(self.signature, 64))
        except BadSignatureError:
            raise ValueError("Invalid offline approval signature") from None
        data = json.loads(decision)
        require(set(data) == {"format", "request_id", "register_id", "event_id", "instance_id", "access_until", "receipt_sha256",
                              "lifecycle_sha256", "event_sha256", "cert_sha256", "destination_id",
                              "issued_at", "valid_until", "copy_status", "disposition"})
        require(all(isinstance(data[k], str) and re.fullmatch("[0-9a-f]{32}", data[k])
                    for k in ("request_id", "register_id")))
        instance = data["instance_id"]
        require(digest(instance))
        validate_binding(event["id"], instance, data["access_until"])
        require(data["format"] == "prod-restore-decision-v1" and data["event_id"] == event["id"]
                and data["event_sha256"] == config_digest and data["cert_sha256"] == cert_digest)
        issued, expires = data["issued_at"], data["valid_until"]
        require(type(issued) is int and type(expires) is int and issued <= now < expires <= issued + MAX_AGE)
        roots = roots_factory(event["id"], instance)
        files = (self.decision, self.signature, self.receipt, self.lifecycle, self.policy)
        require(all(not Path(p).is_relative_to(volume) for volume in roots.volumes for p in files))
        receipt_raw, lifecycle_raw = read_small(self.receipt), read_small(self.lifecycle)
        require(data["receipt_sha256"] == sha(receipt_raw) and data["lifecycle_sha256"] == sha(lifecycle_raw))
        receipt, lifecycle = json.loads(receipt_raw), json.loads(lifecycle_raw)
        require(receipt["format"] == "prod-object-receipt-v1" and receipt["event_id"] == event["id"]
                and receipt["instance_id"] == instance and receipt["archive_sha256"] == archive_digest
                and type(receipt["size"]) is int and receipt["size"] == archive.stat().st_size
                and isinstance(receipt["object_key"], str) and receipt["object_key"].rsplit("/", 1)[-1] == archive.name
                and isinstance(receipt["version_id"], str) and receipt["version_id"] not in ("", "null"))
        require(lifecycle["format"] == "prod-erasure-evidence-v1" and lifecycle["event_id"] == event["id"]
                and lifecycle["instance_id"] == instance and lifecycle["cutoff"] == CUTOFF
                and data["copy_status"] == "available" and lifecycle["backups_expired"] is False
                and type(lifecycle["inventory_unresolved"]) is int and lifecycle["inventory_unresolved"] == 0
                and type(lifecycle["inventory_outstanding"]) is int and lifecycle["inventory_outstanding"] > 0
                and type(lifecycle["closure"]) is bool)
        approved_policy = read_small(self.policy)
        require(lifecycle["approval_sha256"] == sha(approved_policy))
        record = json.loads(approved_policy)
        require(record["format"] == "prod-lifecycle-approval-v1" and record["event_id"] == event["id"]
                and record["instance_id"] == instance and record["access_until"] == CUTOFF)
        policy = record["policy"]
        validate_policy(policy)
        require(policy["retention_days"] == 14 and policy["inventory_approval"])
        created, observed = receipt["created_at"], lifecycle["observed_at"]
        require(type(created) is int and type(observed) is int and 0 < created < CUTOFF
                and created <= observed <= issued and observed >= issued - 14 * DAY)
        purged_at = lifecycle["local_db_purge_verified_at"]
        if purged_at is not None:
            require(type(purged_at) is int and CUTOFF <= purged_at <= observed and lifecycle["closure"])
        disposition = "purged" if purged_at is not None else ("closed" if lifecycle["closure"] else "open")
        require(data["disposition"] == disposition and (observed < CUTOFF or lifecycle["closure"]))
        closed = now >= CUTOFF or lifecycle["closure"]
        if initial and now >= CUTOFF:
            require(observed >= CUTOFF and lifecycle["closure"])
        require(data["destination_id"] == roots.ident(destination, "quarantine" if closed else "restore"))
        if closed:
            require(policy["audit_deletion_approval"] and policy["restore_drill"])
        else:
            require(now < created + 14 * DAY)
        return roots, VerifiedDecision((event["id"], instance, CUTOFF), closed, data["request_id"],
                                       sha(decision), data["register_id"], created)


def consume_decision(register, permit, archive_id, destination_id):
    # Persist before decrypting. An interrupted drill needs a newly signed decision.
    # This table is only created on the recovery workstation's private inventory.
    with register.transaction(*permit.binding) as (db, observed):
        row = db.execute("SELECT register_id FROM recovery_workstation").fetchone()
        require(row is not None and row[0] == permit.register_id)
        # Copying an old archive onto an offline workstation must not reset its
        # 14-day retention clock. Apply the same earlier deadline to its candidate.
        for object_id in (archive_id, destination_id):
            updated = db.execute("UPDATE objects SET created_at=MIN(created_at,?) "
                                 "WHERE object_id=? AND deleted_at IS NULL AND reappeared=0", (permit.created_at, object_id))
            if updated.rowcount != 1:
                raise ValueError("Unregistered or retired recovery copy")
        try:
            db.execute("INSERT INTO restore_approvals VALUES (?,?,?)", (permit.request_id, permit.decision_sha256, observed))
        except sqlite3.IntegrityError:
            raise ValueError("Offline restore decision has already been used") from None


def separate_register(path):
    path = Path(path)
    live_path = Path(os.environ.get("MAISON_PROD_LIFECYCLE", "/var/lib/maison-prod-lifecycle/lifecycle.sqlite"))
    require(path.is_absolute() and path.resolve() == path and path != live_path.resolve())
    for service in ("/var/lib/maison-prod", "/var/lib/maison-prod-lifecycle", "/var/lib/maison-demo"):
        require(not path.is_relative_to(service))
    return path


def recovery_inventory():
    # A Mac or isolated operator machine uses its own receipt/candidate inventory.
    # It can be provisioned from the independently retained event/instance approval
    # after loss of the service host. No live service or remote status call is used.
    path = os.environ.get("MAISON_PROD_RESTORE_REGISTER")
    if not path:
        raise ValueError("A separately provisioned offline restore inventory is required")
    return CustodyLedger(separate_register(path))


def initialize_workstation_identity(register):
    register_id = secrets.token_hex(16)
    with closing(sqlite3.connect(register.path.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE recovery_workstation (register_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO recovery_workstation VALUES (?)", (register_id,))
        db.execute("CREATE TABLE restore_approvals "
                   "(request_id TEXT PRIMARY KEY, decision_sha256 TEXT NOT NULL, attempted_at INTEGER NOT NULL)")
    return register_id


def init_register(path, policy_file, approved_sha256):
    """Initialize an empty recovery workstation inventory from its independent approval."""
    verification_key()
    raw = read_small(policy_file)
    require(digest(approved_sha256) and sha(raw) == approved_sha256)
    record = json.loads(raw)
    require(set(record) == {"format", "event_id", "instance_id", "access_until", "policy"}
            and record["format"] == "prod-lifecycle-approval-v1" and digest(record["instance_id"]))
    binding = record["event_id"], record["instance_id"], record["access_until"]
    validate_binding(*binding)
    validate_policy(record["policy"])
    require(record["policy"]["retention_days"] == 14 and record["policy"]["inventory_approval"])
    register = CustodyLedger.create(separate_register(path), *binding, record["policy"])
    return initialize_workstation_identity(register)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init-register")
    init.add_argument("--register", type=Path, required=True, help="New private inventory outside archive volumes")
    init.add_argument("--lifecycle-policy", type=Path, required=True)
    init.add_argument("--policy-sha256", required=True, help="Policy fingerprint held outside the backup")
    expire = commands.add_parser("expire-register")
    expire.add_argument("--config", type=Path, required=True)
    expire.add_argument("--instance", required=True)
    expire.add_argument("--receipts", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "init-register":
            register_id = init_register(args.register, args.lifecycle_policy, args.policy_sha256)
            print(json.dumps({"register_id": register_id}))
        else:
            import prod
            from prod_backup_custody import Roots, expire as expire_copies
            event = prod.load_prod_event(args.config)
            register = recovery_inventory()
            plan = Roots.from_env(event["id"], args.instance)
            if any(row["service"] for row in plan.volumes.values()):
                raise ValueError("Recovery workstation inventory must exclude active service roots")
            print(json.dumps(expire_copies(plan, register, event["id"], args.instance, CUTOFF, args.receipts)))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        parser.exit(1, "Offline inventory operation failed. Verify the policy, paths, volumes and records.\n")


if __name__ == "__main__":
    main()
