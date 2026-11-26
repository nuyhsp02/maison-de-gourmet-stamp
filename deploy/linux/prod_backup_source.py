"""Local snapshot and custody operations. Only the backup UID can connect to the socket."""
from array import array
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import struct
import sys

import prod
import prod_backup as backup
import prod_backup_custody as custody
import prod_local_lifecycle as lifecycle
from prod_custody import CUTOFF, trusted_file

SOCKET = "/run/maison-prod-backup-source.sock"
NAME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-f0-9]{24}\.cms")
LIMIT = 65536


def configuration():
    path = trusted_file(os.environ["MAISON_PROD_BACKUP_JOB"])
    if path.stat().st_uid != 0 or path.parent.stat().st_uid != 0:
        raise ValueError("Backup job must be operator-owned")
    raw = path.read_bytes()
    backup.matching_digest(hashlib.sha256(raw).hexdigest(), os.environ["MAISON_PROD_BACKUP_JOB_SHA256"])
    cfg = json.loads(raw)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("instance_id"), str):
        raise ValueError("Backup job must pin its production instance")
    return cfg


def binding(cfg):
    event = prod.load_prod_event(cfg["event"])
    return event["id"], cfg["instance_id"], CUTOFF


def archive_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Unexpected backup archive name")
    return name


class SnapshotSource:
    def __init__(self, cfg, ledger, destination_binding):
        self.cfg = cfg
        self.ctx = binding(cfg)
        if "event_id" in cfg and cfg["event_id"] != self.ctx[0]:
            raise ValueError("Backup job and event mismatch")
        self.ledger = ledger
        self.destination_binding = destination_binding
        self.spool = prod.private(backup.detached(cfg["spool"]), directory=True)
        self.proofs = prod.private(backup.detached(cfg["posix_receipts"]), directory=True)
        self.roots = custody.Roots.from_env(*self.ctx[:2])
        # The transfer journal is owned by another UID. Check configured paths lexically
        # here; that UID independently verifies the actual work directory and permissions.
        work = Path(cfg["work"])
        if (not work.is_absolute() or ".." in work.parts or self.spool not in self.roots.volumes
                or not {"archive", "temporary"} <= set(self.roots.volumes[self.spool]["kinds"])
                or not {"external", "snapshot"} <= self.roots.manual
                or any(work.is_relative_to(v) or v.is_relative_to(work) for v in (*self.roots.volumes, self.proofs))):
            raise ValueError("Register isolated job state, spool and custody roots")

    def clock(self):
        with self.ledger.transaction(*self.ctx) as (db, now):
            status = self.ledger._status(db)
        if status.retention_days != 14 or not status.backup_ready:
            raise ValueError("Approved fourteen-day inventory required")
        if status.closed and (now < CUTOFF or not status.purge_authorized):
            raise ValueError("Closed without authority for physical disposition")
        return now, status.closed

    def anchor(self, generation=None):
        if generation is not None and not re.fullmatch("[0-9a-f]{64}", generation):
            raise ValueError("Invalid journal generation")
        with self.ledger.transaction(*self.ctx) as (db, _):
            db.execute("""CREATE TABLE IF NOT EXISTS delivery_anchor
                        (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, generation TEXT NOT NULL)""")
            if generation:
                db.execute("INSERT OR IGNORE INTO delivery_anchor VALUES(1,?,?)",
                           (self.destination_binding, generation))
            row = db.execute("SELECT binding,generation FROM delivery_anchor WHERE id=1").fetchone()
            if row and (row[0] != self.destination_binding or generation and generation != row[1]):
                raise ValueError("Delivery journal binding mismatch")
            return row[1] if row else None

    def expire(self):
        custody.expire(self.roots, self.ledger, *self.ctx, self.proofs)

    def snapshot(self, name):
        name = archive_name(name)
        now, closed = self.clock()
        if closed or not name.startswith(datetime.fromtimestamp(now, prod.KST).date().isoformat() + "-"):
            raise ValueError("Unexpected snapshot date")
        path = self.spool / name
        result = backup.create(Path(self.cfg["state_dir"]), Path(self.cfg["event"]), Path(self.cfg["recipient"]),
                               Path(self.cfg["certificate"]), self.cfg["cert_sha256"], path, authority=self.ledger)
        ident = self.roots.ident(path, "archive")
        row = next(r for r in self.ledger.objects(*self.ctx) if r["object_id"] == ident and r["deleted_at"] is None)
        return row["created_at"], result["archive_sha256"], path.stat().st_size

    def live_archive(self, name):
        path = prod.private(self.spool / archive_name(name))
        backup.require_open(self.ledger, self.ctx, Path(self.cfg["state_dir"]))
        self.ledger.check_archive(*self.ctx, self.roots.ident(path, "archive"))
        return path

    def archive(self, name):
        path = self.live_archive(name)
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    def external(self, name):
        key = f"prod-backups/{self.ctx[0]}/{self.ctx[1]}/" + archive_name(name)
        return hashlib.sha256(("s3:" + self.cfg["data_bucket"] + "/" + key).encode()).hexdigest()

    def reserve(self, name):
        self.live_archive(name)
        self.ledger.reserve(*self.ctx, "external", self.external(name))

    def retired(self, name, proof):
        ident = self.external(name)
        local = self.roots.ident(self.spool / archive_name(name), "archive")
        with self.ledger.transaction(*self.ctx) as (db, now):
            origin = db.execute("SELECT created_at FROM objects WHERE object_id=? AND kind='archive'", (local,)).fetchone()
            if not origin or now < min(origin[0] + 14 * 86400, CUTOFF):
                raise ValueError("Archive is not due for retirement")
            reserved = db.execute("SELECT 1 FROM objects WHERE object_id=? AND kind='external'", (ident,)).fetchone()
        if reserved:
            self.ledger.attest_deleted(*self.ctx, ident, proof)

    def unknown(self, ident):
        if not re.fullmatch("[0-9a-f]{64}", ident):
            raise ValueError("Invalid unknown copy identifier")
        self.ledger.flag_unknown(*self.ctx, ident)

    def clear(self):
        with self.ledger.transaction(*self.ctx) as (db, _):
            if db.execute("SELECT 1 FROM unresolved LIMIT 1").fetchone():
                raise ValueError("Unresolved copy inventory requires custodian review")

    def evidence(self):
        decision = lifecycle.read_approval()
        raw = trusted_file(os.environ["MAISON_PROD_LIFECYCLE_APPROVAL"]).read_bytes()
        pin = os.environ["MAISON_PROD_LIFECYCLE_APPROVAL_SHA256"]
        backup.matching_digest(hashlib.sha256(raw).hexdigest(), pin)
        if json.loads(raw) != decision:
            raise ValueError("Approval changed during export")
        return raw.decode(), lifecycle.evidence(self.ledger, decision)


# Fixed operations, no filenames other than approved-format archive basenames, no
# AWS commands, raw SQL, environment overrides, arbitrary paths or private key reads.
METHODS = ("clock", "anchor", "expire", "snapshot", "archive", "reserve", "retired", "unknown", "clear", "evidence")


def check_peer(sock, expected_uid):
    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != expected_uid:
        raise PermissionError("Unexpected peer UID")


def serve(sock, source, backup_uid):
    check_peer(sock, backup_uid)
    while packet := sock.recv(LIMIT + 1):
        if len(packet) > LIMIT:
            raise ValueError("Oversized source request")
        op, *args = json.loads(packet)
        if op not in METHODS:
            raise ValueError("Unknown source operation")
        value = getattr(source, op)(*args)
        if op == "archive":
            try:
                sock.sendmsg([b'null'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [value]))])
            finally:
                os.close(value)
        else:
            packet = json.dumps(value).encode()
            if len(packet) > LIMIT:
                raise ValueError("Oversized source reply")
            sock.sendall(packet)


class RemoteSource:
    def __init__(self, sock, clock=lifecycle.synchronized_time):
        self.sock = sock
        self.kernel_clock = clock

    def request(self, op, *args):
        self.sock.sendall(json.dumps([op, *args]).encode())
        if op == "archive":
            flags = socket.MSG_CMSG_CLOEXEC
            msg, ancillary, truncated, _ = self.sock.recvmsg(16, socket.CMSG_SPACE(4), flags)
            fds = [fd for level, kind, data in ancillary if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                   for fd in array("i", data)]
            import fcntl
            if (msg == b'null' and not truncated & (socket.MSG_CTRUNC | socket.MSG_TRUNC) and len(fds) == 1
                    and stat.S_ISREG(os.fstat(fds[0]).st_mode)
                    and (fcntl.fcntl(fds[0], fcntl.F_GETFL) & os.O_ACCMODE) == os.O_RDONLY):
                return fds[0]
            for fd in fds:
                os.close(fd)
            raise ValueError("Invalid encrypted archive descriptor")
        msg = self.sock.recv(LIMIT + 1)
        if not msg or len(msg) > LIMIT:
            raise ValueError("Invalid source reply")
        return json.loads(msg)

    def clock(self):
        before = int(self.kernel_clock())
        now, closed = self.request("clock")
        after = int(self.kernel_clock())
        if (type(now) is not int or type(closed) is not bool or not before - 1 <= now <= after
                or closed != (now >= CUTOFF) or (after >= CUTOFF and not closed)):
            raise ValueError("Source and trusted transfer clock differ")
        return now, closed

    def __getattr__(self, operation):
        if operation not in METHODS:
            raise AttributeError(operation)
        return lambda *args: self.request(operation, *args)


@contextmanager
def connect():
    import pwd
    if os.geteuid() != pwd.getpwnam("maison-backup").pw_uid:
        raise PermissionError("Dedicated transfer account required")
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(600)
        sock.connect(SOCKET)
        # systemd owns the listener. It passes the established socket to the source.
        check_peer(sock, 0)
        yield RemoteSource(sock)


if __name__ == "__main__":
    import ctypes
    import pwd
    try:
        os.umask(0o077)
        # Other processes under the data owner's UID must not attach to this
        # systemd-activated helper or reopen its connection through /proc.
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError("Unable to protect the source process")
        if os.geteuid() != pwd.getpwnam("maison-prod").pw_uid:
            raise PermissionError("Designated local source account required")
        from prod_object_backup import destination_binding
        cfg = configuration()
        with socket.socket(fileno=0) as connection:
            connection.settimeout(600)
            check_peer(connection, pwd.getpwnam("maison-backup").pw_uid)
            source = SnapshotSource(cfg, lifecycle.local_ledger(), destination_binding(cfg, binding(cfg)))
            serve(connection, source, pwd.getpwnam("maison-backup").pw_uid)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError, sqlite3.Error):
        sys.exit("Local backup source unavailable")
