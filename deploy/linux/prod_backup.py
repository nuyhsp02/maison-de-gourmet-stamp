"""Back up a verified PROD instance, or recover it to a new offline candidate."""
import argparse
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

import prod
from prod_local_lifecycle import local_ledger
from prod_restore_approval import ApprovalFiles, consume_decision, recovery_inventory
import prod_backup_custody as custody
import prod_purge
from staff_admin import retire_prod_credentials

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import ops

PROFILE_KEY = prod.DATABASE_NAME + ".profile.key"
MANIFEST = "prod-backup-v1.json"
MEMBERS = {prod.DATABASE_NAME, PROFILE_KEY, MANIFEST}
SERVICE_ROOTS = (Path("/var/lib/maison-prod"), Path("/var/lib/maison-demo"),
                 Path("/var/backups/maison-demo"), Path("/etc/maison-demo"))
AUDIT_ORDER = {"products": "id", "stamp_cancel_audit": "id", "stamp_correction_audit": "id",
               "reward_audit": "customer_id", "contact_access_audit": "request_id"}


def detached(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve() or any(path.is_relative_to(p) for p in SERVICE_ROOTS):
        raise ValueError("Use a canonical, separate backup or restore path")
    for parent in path.parents:
        if ((parent / ".git").exists() or (parent / "collection.sqlite").exists() or (parent / prod.DATABASE_NAME).exists()
                or (parent / "instance" / prod.DATABASE_NAME).exists()):
            raise ValueError("Backup and recovery must stay outside source control and service state")
    return path


def exclusive(path):
    return open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600))


def digest_stream(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def matching_digest(actual, expected):
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or not hmac.compare_digest(actual, expected):
        raise ValueError("Digest differs from the separately approved record")


def pinned_certificate(cert, fingerprint, stage):
    # Use exactly the certificate bytes whose DER digest the custodian approved.
    cert = detached(cert)
    der = subprocess.run(["openssl", "x509", "-in", cert, "-outform", "DER"], capture_output=True, check=True).stdout
    matching_digest(hashlib.sha256(der).hexdigest(), fingerprint)
    pem = stage / "recipient.pem"
    with exclusive(pem) as out:
        subprocess.run(["openssl", "x509", "-inform", "DER"], input=der, stdout=out,
                       stderr=subprocess.DEVNULL, check=True)
    return pem


def evidence(path, event, recipient):
    # This is the sealed snapshot or isolated candidate, never the changing live DB.
    with closing(ops.readonly(path)) as db:
        db.execute("BEGIN")
        counts = {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in (*AUDIT_ORDER, "customers", "customer_profiles", "sessions", "stamps", "customer_contacts")}
        fingerprints = {}
        for table, order in AUDIT_ORDER.items():
            digest = hashlib.sha256()
            for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update((json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii"))
            fingerprints[table] = digest.hexdigest()
        instance_id = db.execute("SELECT value FROM metadata WHERE key='prod_instance_id'").fetchone()[0]
    return {"format": "maison-prod-backup-v1", "event_id": event["id"], "instance_id": instance_id,
            "contact_key_id": recipient.key_id, "counts": counts, "fingerprints": fingerprints}


def lifecycle(event, authority):
    authority = authority if authority is not None else local_ledger()
    cutoff = prod.application.timestamp(event['access_until'])
    status = authority.status(event['id'], None, cutoff)
    ctx = (event['id'], status.instance_id, cutoff)
    if int(time.time()) >= cutoff and not status.closed:
        status = authority.close(*ctx)
    if not status.backup_ready or status.retention_days != 14:
        raise ValueError("Approved custody inventory and backup retention are required")
    return authority, ctx, status


def require_open(authority, ctx, state_dir):
    latch = state_dir / ".privacy-closed"
    status = authority.status(*ctx)
    if (latch.exists() or latch.is_symlink() or time.time() >= ctx[2]
            or status.closed or not status.backup_ready):
        raise ValueError("Personal-data backup period is closed")


def create(state_dir, config, recipient_config, cert, cert_sha256, output, *, authority=None):
    state_dir = Path(state_dir)
    output = detached(output)
    prod.private(output.parent, directory=True)
    if output.exists() or output.is_relative_to(state_dir.resolve()):
        raise ValueError("Archive must be a new file outside the state tree")
    event = prod.load_prod_event(config)
    recipient = prod.contact.ProductionEnrollment.read(prod.canonical(recipient_config), event["id"])
    database = prod.database_path(state_dir)
    authority, ctx, _ = lifecycle(event, authority)
    require_open(authority, ctx, state_dir)
    roots = custody.Roots.from_env(event["id"], ctx[1])
    with roots.lock((state_dir, output.parent)):
        key = prod.validate_database(database, event, recipient)
        with closing(ops.readonly(database)) as db:
            if (prod_purge.has_local_purge(db)
                    or db.execute("SELECT value FROM metadata WHERE key='prod_instance_id'").fetchone()[0] != ctx[1]):
                raise ValueError("Backup instance differs from the local inventory")
        stage = state_dir / (".prod-backup-" + secrets.token_hex(16))
        encrypted = output.parent / (".encrypted-" + secrets.token_hex(16) + ".cms")
        for path, kind in ((output, "archive"), (stage, "temporary"), (encrypted, "temporary")):
            authority.reserve(*ctx, kind, roots.ident(path, kind))
        stage.mkdir(mode=0o700)
        try:
            return write_archive(event, recipient, database, key, cert, cert_sha256, output,
                                 stage, encrypted, authority, ctx)
        finally:
            shutil.rmtree(stage)
            prod.sync_dir(state_dir)


def write_archive(event, recipient, database, key, cert, cert_sha256, output, stage, encrypted, authority, ctx):
    pem = pinned_certificate(cert, cert_sha256, stage)
    snapshot = ops.snapshot(database, stage / prod.DATABASE_NAME)
    snapshot.chmod(0o600)
    with exclusive(stage / PROFILE_KEY) as key_file:
        key_file.write(key)
    prod.validate_database(snapshot, event, recipient, require_wal=False)
    record = evidence(snapshot, event, recipient)
    plain = stage / "payload.zip"
    with zipfile.ZipFile(plain, "x") as bundle:
        for name in (prod.DATABASE_NAME, PROFILE_KEY):
            bundle.write(stage / name, arcname=name)
        bundle.writestr(MANIFEST, json.dumps(record, separators=(",", ":")))
    # Only the encrypted stream crosses onto the destination filesystem.
    with open(encrypted, "x+b", opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
        try:
            subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-stream", "-outform", "DER",
                            "-aes-256-gcm", "-recip", pem, "-keyopt", "rsa_padding_mode:oaep",
                            "-keyopt", "rsa_oaep_md:sha256", "-in", plain], stdout=out,
                           stderr=subprocess.DEVNULL, check=True)
            out.flush()
            os.fsync(out.fileno())
            out.seek(0)
            archive_sha256 = digest_stream(out)
            require_open(authority, ctx, stage.parent)
            os.link(encrypted, output)
            try:
                require_open(authority, ctx, stage.parent)
            except BaseException:
                output.unlink()
                prod.sync_dir(output.parent)
                raise
        finally:
            encrypted.unlink()
    prod.sync_dir(output.parent)
    return {"archive_sha256": archive_sha256, "evidence": record}


def extract_candidate(payload, directory):
    with zipfile.ZipFile(payload) as bundle:
        if sorted(bundle.namelist()) != sorted(MEMBERS) or any(i.compress_type != zipfile.ZIP_STORED for i in bundle.infolist()):
            raise ValueError("Invalid PROD archive members")
        if bundle.getinfo(PROFILE_KEY).file_size != 32 or bundle.getinfo(MANIFEST).file_size > 8192:
            raise ValueError("Invalid key or manifest length")
        for name in (PROFILE_KEY, prod.DATABASE_NAME):
            with bundle.open(name) as source, exclusive(directory / name) as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        return json.loads(bundle.read(MANIFEST))


def restore(archive, archive_sha256, config, recipient_config, cert, cert_sha256, identity, destination, *,
            approval=None, authority=None):
    archive, destination = detached(archive), detached(destination)
    prod.private(archive)
    prod.private(archive.parent, directory=True)
    prod.private(destination.parent, directory=True)
    identity = detached(identity)
    prod.private(identity)
    config_digest = hashlib.sha256(prod.canonical(config).read_bytes()).hexdigest()
    event = prod.load_prod_event(config)
    recipient = prod.contact.ProductionEnrollment.read(prod.canonical(recipient_config), event["id"])
    if not isinstance(approval, ApprovalFiles):
        raise ValueError("An independently signed restore decision and both evidence files are required")
    matching_digest(hashlib.sha256(prod.canonical(config).read_bytes()).hexdigest(), config_digest)

    def approved(now, *, initial=False):
        return approval.verify(now, event, config_digest, archive, archive_sha256,
                               cert_sha256, destination, custody.Roots.from_env, initial=initial)

    roots, permit = approved(int(time.time()), initial=True)
    ctx = permit.binding
    authority = authority if authority is not None else recovery_inventory()
    with roots.lock((archive.parent, destination.parent)):
        archive_id = roots.ident(archive, "archive")
        # Only a verified external receipt can enroll an imported CMS in a
        # recovery workstation's inventory. Existing retirement records prevail.
        status = authority.status(*ctx)
        with authority.transaction(*ctx) as (db, observed):
            row = db.execute("SELECT register_id FROM recovery_workstation").fetchone()
            if row is None or row[0] != permit.register_id:
                raise ValueError("Signed decision names another workstation inventory")
            _, checked = approved(max(observed, int(time.time())), initial=True)
            if checked.decision_sha256 != permit.decision_sha256 or (status.closed and not checked.closed):
                raise ValueError("Independent evidence does not attest the current closure")
        if not status.backup_ready or status.retention_days != 14:
            raise ValueError("Offline retention and custody scope are not approved")
        if checked.closed:
            authority.close(*ctx)
        if not any(row["object_id"] == archive_id for row in authority.objects(*ctx)):
            authority.discover(*ctx, "archive", archive_id)
        if destination.exists():
            raise ValueError("Recovery requires a new isolated candidate")

        def check_archive():
            with authority.transaction(*ctx) as (_, now):
                _, checked = approved(max(now, int(time.time())))
            if checked.decision_sha256 != permit.decision_sha256:
                raise ValueError("Restore approval binding changed")
            if checked.closed:
                authority.close(*ctx)
            return authority.check_archive(*ctx, archive_id)

        kind = "quarantine" if check_archive().closed else "restore"
        destination_id = roots.ident(destination, kind)
        authority.reserve(*ctx, kind, destination_id)
        consume_decision(authority, permit, archive_id, destination_id)
        return restore_candidate(archive, archive_sha256, event, recipient, cert, cert_sha256, identity,
                                 destination, authority, ctx, check_archive)


def restore_candidate(archive, archive_sha256, event, recipient, cert, cert_sha256, identity, destination,
                      authority, ctx, check_archive):
    # Exclusive reservation, even if another recovery or an existing state is empty.
    destination.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(dir=destination, prefix=".verify-") as tmp:
            stage = Path(tmp)
            pem = pinned_certificate(cert, cert_sha256, stage)
            payload = stage / "payload.zip"
            # A versioned-object download remains caller-owned. Decrypt the exact verified
            # bytes from a protected stage even if that download changes during the drill.
            staged_cms = stage / "snapshot.cms"
            with archive.open("rb") as source, exclusive(staged_cms) as target:
                shutil.copyfileobj(source, target)
            with staged_cms.open("rb") as sealed, exclusive(payload) as out:
                matching_digest(digest_stream(sealed), archive_sha256)
                sealed.seek(0)
                check_archive()
                subprocess.run(["openssl", "cms", "-decrypt", "-binary", "-inform", "DER", "-recip", pem,
                                "-inkey", identity], stdin=sealed, stdout=out, stderr=subprocess.DEVNULL, check=True)
            staged_cms.unlink()
            candidate = stage / "instance"
            candidate.mkdir(mode=0o700)
            manifest = extract_candidate(payload, candidate)
            database = candidate / prod.DATABASE_NAME
            prod.validate_database(database, event, recipient, require_wal=False)
            if manifest.get("instance_id") != ctx[1] or evidence(database, event, recipient) != manifest:
                raise ValueError("Restored counts, audit or inventory differ from the snapshot")
            with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    retire_prod_credentials(db)
                if db.execute("SELECT 1 FROM admin_sessions LIMIT 1").fetchone():
                    raise ValueError("Privileged sessions must be retired")
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise ValueError("Could not enable PROD WAL")
            # Revoking privileges must not alter any business record or audit.
            prod.validate_database(database, event, recipient)
            if evidence(database, event, recipient) != manifest:
                raise ValueError("Credential retirement changed protected records")
            if set(item.name for item in candidate.iterdir()) - {prod.DATABASE_NAME, PROFILE_KEY}:
                # Read-only validation in WAL mode may create fresh WAL/SHM; a
                # clean last writer closes and removes these isolated sidecars.
                with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db:
                    if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        raise ValueError("Recovery candidate still busy")
            if {item.name for item in candidate.iterdir()} != {prod.DATABASE_NAME, PROFILE_KEY}:
                raise ValueError("Unexpected files in recovery candidate")
            # Isolation holds through both checks. Running this candidate later also
            # requires its local lifecycle gate and independent credential issuance.
            status = check_archive()
            if int(time.time()) >= ctx[2] and not status.closed:
                status = authority.close(*ctx)
            sanitized = status.closed
            if sanitized:
                prod_purge.purge(stage, event, recipient, authority)
            payload.unlink()
            with database.open("rb") as file:
                os.fsync(file.fileno())
            prod.sync_dir(candidate)
            candidate.rename(destination / "instance")
        status = check_archive()
        if not sanitized and (status.closed or time.time() >= ctx[2]):
            if not status.closed:
                authority.close(*ctx)
            prod_purge.purge(destination, event, recipient, authority)
            sanitized = True
        prod.sync_dir(destination)
        prod.sync_dir(destination.parent)
    except BaseException:
        shutil.rmtree(destination)
        raise
    if sanitized:
        return {"archive_sha256": archive_sha256, "sanitized": True}
    return {"archive_sha256": archive_sha256, "evidence": manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    make = actions.add_parser("create")
    make.add_argument("--state-dir", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    stage = actions.add_parser("restore")
    stage.add_argument("--approval", type=Path, required=True, help="Offline custodian's signed decision JSON")
    stage.add_argument("--signature", type=Path, required=True, help="Detached Ed25519 signature")
    stage.add_argument("--receipt", type=Path, required=True, help="Independently retained object receipt")
    stage.add_argument("--lifecycle-evidence", type=Path, required=True, help="Fresh independent disposition record")
    stage.add_argument("--lifecycle-policy", type=Path, required=True, help="Archived approval referenced by lifecycle evidence")
    stage.add_argument("--archive", type=Path, required=True)
    stage.add_argument("--sha256", required=True, help="independently recorded CMS file SHA-256")
    stage.add_argument("--identity", type=Path, required=True, help="off-host backup identity")
    stage.add_argument("--destination", type=Path, required=True)
    for action in (make, stage):
        action.add_argument("--config", type=Path, required=True)
        action.add_argument("--recipient", type=Path, required=True, help="PROD contact public descriptor")
        action.add_argument("--recipient-cert", type=Path, required=True, help="dedicated PROD backup public certificate")
        action.add_argument("--cert-sha256", required=True, help="approved backup certificate DER SHA-256")
    args = parser.parse_args()
    os.umask(0o077)
    common = (args.config, args.recipient, args.recipient_cert, args.cert_sha256)
    try:
        if args.action == "create":
            result = create(args.state_dir, *common, args.output)
        else:
            approval = ApprovalFiles(args.approval, args.signature, args.receipt, args.lifecycle_evidence, args.lifecycle_policy)
            result = restore(args.archive, args.sha256, *common, args.identity, args.destination, approval=approval)
    except (OSError, TypeError, ValueError, RuntimeError, KeyError, sqlite3.Error, ops.OpsError,
            zipfile.BadZipFile, subprocess.CalledProcessError):
        parser.exit(1, "PROD backup failed. Check isolated paths, event, credentials and trusted fingerprints.\n")
    # Detailed row fingerprints belong inside the encrypted snapshot only.
    print(json.dumps({key: value for key, value in result.items() if key in ("archive_sha256", "sanitized")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
