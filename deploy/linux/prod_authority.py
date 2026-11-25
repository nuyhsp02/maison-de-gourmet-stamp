"""Independent PROD custody ledger and mutually authenticated live status client.

Only the custodian host opens the ledger. Application/backup hosts may read
status, latch closure, reserve copies and report live erasure. They cannot
approve a retention period, assert a deletion receipt or clear a closure.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import stat
import time
from urllib.parse import urlsplit


CUTOFF = int(datetime.fromisoformat("2027-03-31T00:00:00+09:00").timestamp())
INVENTORY_KINDS = ("local", "external", "archive", "restore", "snapshot", "temporary", "previous", "quarantine")
APPROVALS = ("retention_approval", "audit_deletion_approval", "restore_drill", "inventory_approval")
MAX_BODY = 8192
TIMEOUT = 3


class AuthorityError(ValueError):
    """A non-sensitive error safe for application logs."""


@dataclass(frozen=True)
class Status:
    instance_id: str
    closed: bool
    purge_authorized: bool
    backups_expired: bool
    purged: bool
    ready: bool
    retention_days: int | None
    backup_ready: bool = False


def digest(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def trusted_file(value, *, private=False):
    path = Path(value)
    info = path.lstat()
    if any(
        (parent / ".git").exists()
        or (parent / "instance" / "prod.sqlite").exists()
        or (parent / "prod.sqlite").exists()
        for parent in path.parents
    ):
        raise AuthorityError("Custody trust files must be outside source and recoverable application state")
    if path.parent.stat().st_mode & 0o022:
        raise AuthorityError("Custody file directory is writable by other principals")
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & (0o077 if private else 0o022)
    ):
        raise AuthorityError("Noncanonical or unprotected custody file")
    return path


def validate_binding(event_id, instance_id, cutoff):
    if (
        not isinstance(event_id, str)
        or not re.fullmatch(r"prod-[a-z0-9][a-z0-9-]{0,58}", event_id)
        or (instance_id is not None and not digest(instance_id))
        or type(cutoff) is not int
        or cutoff != CUTOFF
    ):
        raise AuthorityError("Invalid custody binding")


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {*APPROVALS, "retention_days"}:
        raise AuthorityError("Incomplete custody decision")
    days = policy["retention_days"]
    if days is not None and (type(days) is not int or days <= 0 or days > 2**31 - 1):
        raise AuthorityError("Invalid daily retention")
    if any(policy[k] is not None and not digest(policy[k]) for k in APPROVALS):
        raise AuthorityError("Invalid approval evidence reference")
    if (days is None) != (policy["retention_approval"] is None):
        raise AuthorityError("Retention requires a separate approval")


class CustodyLedger:
    """One new event per ledger, on independently administered durable storage."""

    def __init__(self, path, *, clock=None):
        self.path = trusted_file(path, private=True)
        self.clock = clock if clock is not None else time.time
        self._directory(self.path)
        info = self.path.stat()
        self.file_identity = (info.st_dev, info.st_ino)

    @staticmethod
    def _directory(path):
        path = Path(path)
        parent = path.parent
        if (
            not path.is_absolute()
            or parent.resolve(strict=True) != parent
            or parent.is_symlink()
            or parent.stat().st_uid != os.geteuid()
            or stat.S_IMODE(parent.stat().st_mode) != 0o700
            or any(
                (p / ".git").exists() or (p / "instance" / "prod.sqlite").exists() or (p / "prod.sqlite").exists()
                for p in (parent, *parent.parents)
            )
        ):
            raise AuthorityError("Independent private custody directory required")

    @classmethod
    def create(cls, path, event_id, instance_id, cutoff, policy, *, clock=None):
        path = Path(path)
        cls._directory(path)
        validate_binding(event_id, instance_id, cutoff)
        if instance_id is None:
            raise AuthorityError("A prepared PROD instance is required")
        validate_policy(policy)
        with open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)):
            pass
        ledger = cls(path, clock=clock)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                """
                CREATE TABLE event (
                    event_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, cutoff INTEGER NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0,
                    clock_high_water INTEGER NOT NULL DEFAULT 0,
                    live_erased INTEGER NOT NULL DEFAULT 0, live_receipt_at INTEGER,
                    policy TEXT NOT NULL
                );
                CREATE TABLE objects (
                    object_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at INTEGER NOT NULL,
                    deleted_at INTEGER, receipt_sha256 TEXT, reappeared INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE deletion_history(object_id TEXT NOT NULL, observed_at INTEGER NOT NULL, receipt_sha256 TEXT NOT NULL);
                CREATE TABLE unresolved(object_id TEXT PRIMARY KEY, observed_at INTEGER NOT NULL);
                CREATE TABLE sources(kind TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, receipt_sha256 TEXT NOT NULL);
            """
            )
            db.execute(
                "INSERT INTO event(event_id,instance_id,cutoff,policy) VALUES (?,?,?,?)",
                (event_id, instance_id, cutoff, json.dumps(policy, sort_keys=True)),
            )
        return ledger

    @contextmanager
    def transaction(self, event_id, instance_id, cutoff, *, lookup=False):
        validate_binding(event_id, instance_id, cutoff)
        if instance_id is None and not lookup:
            raise AuthorityError("Exact instance identity required")
        info = trusted_file(self.path, private=True).stat()
        if self.file_identity != (info.st_dev, info.st_ino):
            raise AuthorityError("Custody ledger was replaced during service")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=TIMEOUT)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM event").fetchone()
            if (
                row is None
                or row["event_id"] != event_id
                or row["cutoff"] != cutoff
                or instance_id not in (None, row["instance_id"])
            ):
                raise AuthorityError("Event or instance differs from independent record")
            now = max(row["clock_high_water"], int(self.clock()))
            if now > row["clock_high_water"]:
                db.execute(
                    "UPDATE event SET clock_high_water=?,closed=MAX(closed,?),due=MAX(due,?)",
                    (now, now >= cutoff, now >= cutoff),
                )
                # Both daily expiry and the cutoff survive a refused operation or a clock step back.
                db.commit()
                db.execute("BEGIN IMMEDIATE")
                now = db.execute("SELECT clock_high_water FROM event").fetchone()[0]
            yield db, now

    @staticmethod
    def _status(db):
        row = db.execute("SELECT * FROM event").fetchone()
        policy = json.loads(row["policy"])
        validate_policy(policy)
        ready = all(policy.values())
        closed = bool(row["closed"])
        authorized = bool(
            closed
            and row["due"]
            and policy["audit_deletion_approval"]
            and policy["restore_drill"]
            and policy["inventory_approval"]
        )
        reconciled = {r[0] for r in db.execute("SELECT kind FROM sources")}
        active = db.execute("SELECT 1 FROM objects WHERE deleted_at IS NULL LIMIT 1").fetchone()
        unknown = db.execute("SELECT 1 FROM unresolved LIMIT 1").fetchone()
        expired = bool(
            closed
            and policy["inventory_approval"]
            and active is None
            and unknown is None
            and reconciled == set(INVENTORY_KINDS)
        )
        return Status(
            row["instance_id"],
            closed,
            authorized,
            expired,
            bool(row["live_erased"]),
            ready,
            policy["retention_days"],
            bool(policy["retention_approval"] and policy["inventory_approval"]),
        )

    def status(self, event_id, instance_id, cutoff):
        with self.transaction(event_id, instance_id, cutoff, lookup=True) as (db, _):
            return self._status(db)

    def close(self, event_id, instance_id, cutoff):
        # A caller that has observed its cutoff may only move toward denial.
        if not digest(instance_id):
            raise AuthorityError("Instance required to latch")
        with self.transaction(event_id, instance_id, cutoff) as (db, _):
            db.execute("UPDATE event SET closed=1")
            return self._status(db)

    def record_purge(self, event_id, instance_id, cutoff):
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            if not digest(instance_id) or not self._status(db).purge_authorized:
                raise AuthorityError("Purge receipt unavailable")
            db.execute("UPDATE event SET live_erased=1,live_receipt_at=? WHERE live_erased=0", (now,))
            return self._status(db)

    @staticmethod
    def _reserve(db, kind, object_id, now):
        if kind not in INVENTORY_KINDS or not digest(object_id):
            raise AuthorityError("Invalid inventory item")
        existing = db.execute("SELECT kind,deleted_at FROM objects WHERE object_id=?", (object_id,)).fetchone()
        if existing:
            if existing["kind"] != kind or existing["deleted_at"] is not None:
                raise AuthorityError("Inventory identity already used")
            return
        db.execute("INSERT INTO objects(object_id,kind,created_at) VALUES (?,?,?)", (object_id, kind, now))
        db.execute("DELETE FROM sources WHERE kind=?", (kind,))

    def reserve(self, event_id, instance_id, cutoff, kind, object_id):
        if not digest(instance_id):
            raise AuthorityError("Instance required for reserve")
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            status = self._status(db)
            if not status.backup_ready or (
                status.closed and (kind != "quarantine" or not status.purge_authorized)
            ):
                raise AuthorityError("Copy reservation unavailable")
            self._reserve(db, kind, object_id, now)
            return self._status(db)

    def check_archive(self, event_id, instance_id, cutoff, object_id):
        if not digest(object_id):
            raise AuthorityError("Registered archive required")
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            status = self._status(db)
            row = db.execute(
                "SELECT kind,created_at,deleted_at,reappeared FROM objects WHERE object_id=?", (object_id,)
            ).fetchone()
            if (
                not status.backup_ready
                or row is None
                or row["kind"] != "archive"
                or row["deleted_at"] is not None
                or row["reappeared"]
                or (not status.closed and now >= row["created_at"] + status.retention_days * 86400)
            ):
                raise AuthorityError("Archive is unknown, retired or past daily retention")
            return status

    def flag_unknown(self, event_id, instance_id, cutoff, object_id):
        if not digest(object_id):
            raise AuthorityError("Opaque unexpected item identity required")
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            db.execute("INSERT OR IGNORE INTO unresolved VALUES (?,?)", (object_id, now))
            return self._status(db)

    def discover(self, event_id, instance_id, cutoff, kind, object_id):
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            row = db.execute("SELECT kind,deleted_at FROM objects WHERE object_id=?", (object_id,)).fetchone()
            if row and row["kind"] == kind and row["deleted_at"] is not None:
                # A listing contradicted earlier evidence. Keep the first receipt;
                # a second discovery suspends expiry and can never be restored.
                db.execute(
                    "UPDATE objects SET reappeared=1,deleted_at=NULL,receipt_sha256=NULL WHERE object_id=?",
                    (object_id,),
                )
                db.execute("DELETE FROM sources WHERE kind=?", (kind,))
            else:
                self._reserve(db, kind, object_id, now)
            db.execute("DELETE FROM unresolved WHERE object_id=?", (object_id,))
            return self._status(db)

    def objects(self, event_id, instance_id, cutoff):
        with self.transaction(event_id, instance_id, cutoff) as (db, _):
            return [dict(row) for row in db.execute("SELECT * FROM objects ORDER BY object_id")]

    def attest_deleted(self, event_id, instance_id, cutoff, object_id, receipt_sha256):
        if not digest(object_id) or not digest(receipt_sha256):
            raise AuthorityError("Independent deletion receipt required")
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            current = db.execute("SELECT * FROM objects WHERE object_id=?", (object_id,)).fetchone()
            if current is None or current["receipt_sha256"] not in (None, receipt_sha256):
                raise AuthorityError("Unknown object or conflicting receipt")
            if current["deleted_at"] is None:
                db.execute("INSERT INTO deletion_history VALUES (?,?,?)", (object_id, now, receipt_sha256))
                db.execute(
                    "UPDATE objects SET deleted_at=?,receipt_sha256=? WHERE object_id=?",
                    (now, receipt_sha256, object_id),
                )
            return self._status(db)

    def reconcile(self, event_id, instance_id, cutoff, kind, receipt_sha256):
        if kind not in INVENTORY_KINDS or not digest(receipt_sha256):
            raise AuthorityError("Full source listing receipt required")
        with self.transaction(event_id, instance_id, cutoff) as (db, now):
            if (
                not db.execute("SELECT due FROM event").fetchone()[0]
                or db.execute(
                    "SELECT 1 FROM objects WHERE kind=? AND deleted_at IS NULL LIMIT 1", (kind,)
                ).fetchone()
            ):
                raise AuthorityError("Independent source is not drained")
            db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?)", (kind, now, receipt_sha256))
            return self._status(db)

    def approve(self, event_id, instance_id, cutoff, policy):
        validate_policy(policy)
        with self.transaction(event_id, instance_id, cutoff) as (db, _):
            old = json.loads(db.execute("SELECT policy FROM event").fetchone()[0])
            if any(value is not None and policy[key] != value for key, value in old.items()):
                raise AuthorityError("Existing approvals are immutable")
            db.execute("UPDATE event SET policy=?", (json.dumps(policy, sort_keys=True),))
            return self._status(db)


ACTIONS = {
    "status": (),
    "close": (),
    "record_purge": (),
    "reserve": ("kind", "object_id"),
    "check_archive": ("object_id",),
}


def make_server(ledger, address, *, cert, key, client_ca, live_peer_sha256=None):
    """Listen only on the custodian host; use dedicated CA for the permitted peer identities."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (
                    self.path != "/v1/lifecycle"
                    or not (0 < size <= MAX_BODY)
                    or self.headers.get("Transfer-Encoding")
                ):
                    raise AuthorityError("Invalid request")
                data = json.loads(self.rfile.read(size))
                action = data["action"]
                if (
                    not digest(data["nonce"])
                    or action not in ACTIONS
                    or set(data) != {"nonce", "action", "event_id", "instance_id", "cutoff", *ACTIONS[action]}
                ):
                    raise AuthorityError("Invalid request")
                binding = (data["event_id"], data["instance_id"], data["cutoff"])
                if action == "record_purge" or (action == "close" and not ledger.status(*binding).closed):
                    peer_digest = hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
                    if not digest(live_peer_sha256) or not hmac.compare_digest(peer_digest, live_peer_sha256):
                        raise AuthorityError("Active instance certificate required")
                arguments = [data[k] for k in ("event_id", "instance_id", "cutoff", *ACTIONS[action])]
                status = getattr(ledger, action)(*arguments)
                result = {
                    "nonce": data["nonce"],
                    "event_id": data["event_id"],
                    "cutoff": data["cutoff"],
                    "status": asdict(status),
                }
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (KeyError, ValueError, TypeError, sqlite3.Error, OSError):
                self.send_error(403, "Custody check refused")

        def log_message(self, *_):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(trusted_file(client_ca)))
    context.load_cert_chain(trusted_file(cert), trusted_file(key, private=True))

    class Server(ThreadingHTTPServer):
        # Do not block the accept loop on an unauthenticated TLS handshake.
        def get_request(self):
            stream, peer = super().get_request()
            stream.settimeout(TIMEOUT)
            try:
                return context.wrap_socket(stream, server_side=True, do_handshake_on_connect=False), peer
            except BaseException:
                stream.close()
                raise

        def handle_error(self, request, client_address):
            pass

    return Server(address, Handler)


class Authority:
    def __init__(self, descriptor):
        try:
            cfg = json.loads(trusted_file(descriptor).read_bytes())
            if set(cfg) != {"url", "ca", "cert", "key"}:
                raise AuthorityError("Malformed authority descriptor")
            url = urlsplit(cfg["url"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise AuthorityError("Dedicated HTTPS authority origin required")
            self.host, self.port = url.hostname, url.port or 443
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.context.load_verify_locations(cafile=str(trusted_file(cfg["ca"])))
            self.context.load_cert_chain(trusted_file(cfg["cert"]), trusted_file(cfg["key"], private=True))
        except (ValueError, OSError, TypeError, KeyError):
            raise AuthorityError("Independent lifecycle authority configuration unavailable") from None

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("MAISON_PROD_AUTHORITY", ""))

    def _request(self, action, event_id, instance_id, cutoff, **extra):
        validate_binding(event_id, instance_id, cutoff)
        nonce = secrets.token_hex(32)
        data = dict(
            action=action, event_id=event_id, instance_id=instance_id, cutoff=cutoff, nonce=nonce, **extra
        )
        connection = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=TIMEOUT)
        try:
            connection.request("POST", "/v1/lifecycle", json.dumps(data), {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            if response.status != 200 or len(raw) > MAX_BODY:
                raise AuthorityError("Custody request refused")
            reply = json.loads(raw)
            s = Status(**reply["status"])
            if (
                not hmac.compare_digest(nonce, reply["nonce"])
                or reply["event_id"] != event_id
                or reply["cutoff"] != cutoff
                or not digest(s.instance_id)
                or instance_id not in (None, s.instance_id)
                or any(
                    type(getattr(s, k)) is not bool
                    for k in ("ready", "backup_ready", "purged", "backups_expired", "closed", "purge_authorized")
                )
                or (s.retention_days is not None and (type(s.retention_days) is not int or s.retention_days <= 0))
                or (s.ready and not s.backup_ready)
                or (s.backup_ready and s.retention_days is None)
                or ((s.purged or s.backups_expired or s.purge_authorized) and not s.closed)
            ):
                raise AuthorityError("Custody proof mismatch")
            return s
        except (ValueError, OSError, TypeError, KeyError, http.client.HTTPException):
            raise AuthorityError("Live custody proof unavailable") from None
        finally:
            connection.close()

    def status(self, event_id, instance_id, cutoff):
        return self._request("status", event_id, instance_id, cutoff)

    def close(self, event_id, instance_id, cutoff):
        return self._request("close", event_id, instance_id, cutoff)

    def reserve(self, event_id, instance_id, cutoff, kind, object_id):
        return self._request("reserve", event_id, instance_id, cutoff, kind=kind, object_id=object_id)

    def check_archive(self, event_id, instance_id, cutoff, object_id):
        return self._request("check_archive", event_id, instance_id, cutoff, object_id=object_id)

    def record_purge(self, event_id, instance_id, cutoff):
        return self._request("record_purge", event_id, instance_id, cutoff)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument(
        "--live-peer-sha256", required=True, help="Independently pinned active app client certificate DER SHA-256"
    )
    for key in ("cert", "key", "client-ca"):
        serve.add_argument("--" + key, type=Path, required=True)
    for name in ("create", "approve", "status", "objects", "discover", "flag-unknown", "attest", "reconcile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--event", required=True)
        cmd.add_argument("--instance", required=True)
        if name in ("create", "approve"):
            cmd.add_argument("--policy", type=Path, required=True)
        if name in ("discover", "reconcile"):
            cmd.add_argument("--kind", choices=INVENTORY_KINDS, required=True)
        if name in ("discover", "flag-unknown", "attest"):
            cmd.add_argument("--object-id", required=True)
        if name in ("attest", "reconcile"):
            cmd.add_argument("--receipt-sha256", required=True)
            cmd.add_argument("--receipt", type=Path, required=True, help="Custodian-verified provider evidence")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "serve":
            with make_server(
                CustodyLedger(args.ledger),
                (args.listen, args.port),
                cert=args.cert,
                key=args.key,
                client_ca=args.client_ca,
                live_peer_sha256=args.live_peer_sha256,
            ) as server:
                server.serve_forever()
            return
        binding = (args.event, args.instance, CUTOFF)
        if args.action in {"attest", "reconcile"}:
            with trusted_file(args.receipt, private=True).open("rb") as proof:
                receipt_hash = hashlib.sha256()
                for chunk in iter(lambda: proof.read(65536), b""):
                    receipt_hash.update(chunk)
                receipt = receipt_hash.hexdigest()
            if not hmac.compare_digest(receipt, args.receipt_sha256):
                raise AuthorityError("External evidence differs from the independent receipt")
        if args.action in ("create", "approve"):
            policy = json.loads(trusted_file(args.policy).read_bytes())
            if args.action == "create":
                ledger = CustodyLedger.create(args.ledger, *binding, policy)
                result = ledger.status(*binding)
            else:
                result = CustodyLedger(args.ledger).approve(*binding, policy)
        else:
            ledger = CustodyLedger(args.ledger)
            if args.action == "discover":
                result = ledger.discover(*binding, args.kind, args.object_id)
            elif args.action == "flag-unknown":
                result = ledger.flag_unknown(*binding, args.object_id)
            elif args.action == "attest":
                result = ledger.attest_deleted(*binding, args.object_id, args.receipt_sha256)
            elif args.action == "reconcile":
                result = ledger.reconcile(*binding, args.kind, args.receipt_sha256)
            else:
                result = getattr(ledger, args.action)(*binding)
        print(json.dumps(result if isinstance(result, list) else asdict(result), sort_keys=True))
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
        parser.exit(1, "Custody operation refused. Verify protected inputs and independent records.\n")


if __name__ == "__main__":
    main()
