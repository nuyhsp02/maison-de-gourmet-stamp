"""Create or stage an encrypted backup of one provisioned Linux DEMO."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile

from demo import DATABASE_NAME, load_demo_event, validate_database

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import ops

PROFILE_KEY = DATABASE_NAME + ".profile.key"
LIVE_STATE = Path("/var/lib/maison-demo")


def private_file(path):
    return open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600))


def summary(database):
    # Only counts leave the private directory. QR keys and audit actors stay in SQLite.
    with closing(ops.readonly(database)) as db:
        tables = ["products", "customers", "customer_profiles", "stamps", "reward_audit", "stamp_cancel_audit"]
        if db.execute("SELECT 1 FROM metadata WHERE key='contact_recipient_v1'").fetchone():
            tables.extend(("customer_contacts", "contact_access_audit"))
        return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in tables}


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create(state_dir, event_path, cert, output):
    if output.is_symlink() or output.exists() or output.resolve().is_relative_to(state_dir.resolve()):
        raise ValueError("Use a new archive outside the service state directory")
    event = load_demo_event(event_path)
    source = state_dir / DATABASE_NAME
    key = validate_database(source, event)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".demo-backup-") as tmp:
        stage = Path(tmp)
        snapshot = ops.snapshot(source, stage / DATABASE_NAME, event=event)
        snapshot.chmod(0o600)
        with private_file(stage / PROFILE_KEY) as stream:
            stream.write(key)
        # A snapshot and a key read at different instants must still form the same DEMO.
        validate_database(snapshot, event)
        counts = summary(snapshot)
        package = stage / "pair.zip"
        with zipfile.ZipFile(package, "x") as bundle:
            for name in (DATABASE_NAME, PROFILE_KEY):
                bundle.write(stage / name, arcname=name)
        encrypted = stage / "sealed.cms"
        with private_file(encrypted) as stream:
            subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-stream", "-outform", "DER",
                            "-aes-256-gcm", "-recip", cert, "-keyopt", "rsa_padding_mode:oaep",
                            "-keyopt", "rsa_oaep_md:sha256", "-in", package], stdout=stream, check=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(encrypted, output)  # Atomic publication; an existing name always wins.
        sync_directory(output.parent)
    return counts


def restore(archive, event_path, cert, identity, destination):
    if destination.resolve().is_relative_to(LIVE_STATE.resolve()):
        raise ValueError("Use an isolated restore directory")
    identity_stat = identity.lstat()
    if not stat.S_ISREG(identity_stat.st_mode) or identity_stat.st_mode & 0o077:
        raise ValueError("The decryption key must be a private regular file")
    event = load_demo_event(event_path)
    # mkdir is exclusive, even for an existing empty directory or competing restore.
    destination.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(dir=destination, prefix=".verify-") as tmp:
            package = Path(tmp) / "pair.zip"
            with private_file(package) as stream:
                subprocess.run(["openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
                                "-in", archive, "-recip", cert, "-inkey", identity],
                               stdout=stream, check=True)
            # Read fixed byte streams only; archive paths, permissions and links are never installed.
            with zipfile.ZipFile(package) as bundle:
                if sorted(bundle.namelist()) != sorted((DATABASE_NAME, PROFILE_KEY)):
                    raise ValueError("Invalid DEMO bundle members")
                if bundle.getinfo(PROFILE_KEY).file_size != 32:
                    raise ValueError("Invalid profile key length")
                for name in (PROFILE_KEY, DATABASE_NAME):
                    if bundle.getinfo(name).compress_type != zipfile.ZIP_STORED:
                        raise ValueError("Unexpected compressed bundle member")
                    with bundle.open(name) as source, private_file(destination / name) as target:
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
        database = destination / DATABASE_NAME
        ops.inspect_database(database, event)
        validate_database(database, event)
        counts = summary(database)
        # ops.snapshot produces a single transportable DELETE-mode file. Return the
        # validated candidate to the WAL mode used by the service before handoff.
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)) as db:
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("Could not restore DEMO WAL mode")
        sync_directory(destination)
        sync_directory(destination.parent)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    make = commands.add_parser("create")
    make.add_argument("--state-dir", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    stage = commands.add_parser("restore")
    stage.add_argument("--archive", type=Path, required=True)
    stage.add_argument("--identity", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    for command in (make, stage):
        command.add_argument("--event", type=Path, required=True)
        command.add_argument("--recipient-cert", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "create":
            counts = create(args.state_dir, args.event, args.recipient_cert, args.output)
        else:
            counts = restore(args.archive, args.event, args.recipient_cert, args.identity, args.destination)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ops.OpsError,
            zipfile.BadZipFile, subprocess.CalledProcessError):
        parser.exit(1, "DEMO backup operation failed. Check input, credentials, permissions, space and DB/key pairing.\n")
    print(json.dumps({"status": "ok", "counts": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
