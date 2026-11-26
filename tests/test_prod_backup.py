"""Offline CMS restore drills for a new, synthetic PROD-format instance."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import zipfile

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private Linux PROD files and OpenSSL 3")


@pytest.fixture(scope="module")
def recipient_cert(tmp_path_factory):
    custody = tmp_path_factory.mktemp("synthetic-backup-custody").resolve()
    cert, identity = custody / "fixture.pem", custody / "identity.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", identity, "-out", cert, "-subj", "/CN=synthetic-backup-drill"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    identity.chmod(0o600)
    der = subprocess.check_output(["openssl", "x509", "-in", cert, "-outform", "DER"])
    return cert, hashlib.sha256(der).hexdigest(), identity


@pytest.fixture
def instance(tmp_path, monkeypatch):
    from prod_fixtures import ORIGIN, make_config
    import prod
    from contact_privacy import ProductionEnrollment

    root = tmp_path.resolve()
    config, descriptor = make_config(root)
    event = prod.load_prod_event(config)
    recipient = ProductionEnrollment.read(descriptor, event["id"])
    state = root / "service"
    state.mkdir(mode=0o700)
    prod.prepare(state, config, ORIGIN, recipient)
    for directory in ("prod-vault", "offline"):
        (root / directory).mkdir(mode=0o700)
    from prod_authority import APPROVALS, CUTOFF, CustodyLedger
    import prod_backup as backup
    import prod_backup_custody as custody
    from types import SimpleNamespace
    import prod_purge

    with closing(sqlite3.connect(prod.database_path(state))) as db:
        instance_id = db.execute("SELECT value FROM metadata WHERE key='prod_instance_id'").fetchone()[0]
    custodian = root / "synthetic-independent-custodian"
    custodian.mkdir(mode=0o700)
    clock = [CUTOFF - 16 * 86400]
    ledger = CustodyLedger.create(custodian / "ledger.sqlite", event["id"], instance_id, CUTOFF,
                                  {**dict.fromkeys(APPROVALS, "c" * 64), "retention_days": 14},
                                  clock=lambda: clock[0])
    monkeypatch.setattr(backup, "local_ledger", lambda: ledger)
    monkeypatch.setattr(backup, "recovery_inventory", lambda: ledger)
    test_time = SimpleNamespace(time=lambda: clock[0])
    for module in (backup, prod_purge, prod):
        monkeypatch.setattr(module, "time", test_time)
    from prod_restore_approval import initialize_workstation_identity
    ledger.fixture_register_id = initialize_workstation_identity(ledger)
    ledger.fixture_time = clock
    volumes = []
    for label, folder, kinds in (("1", state, ["temporary"]),
                                 ("2", root / "prod-vault", ["archive", "temporary"]),
                                 ("3", root / "offline", ["restore", "quarantine", "previous", "snapshot", "local", "external"])):
        volume_id = label * 32
        service = folder == state
        if not service:
            marker = folder / custody.MARKER
            marker.write_text(volume_id)
            marker.chmod(0o600)
        volumes.append(dict(volume_id=volume_id, path=str(folder), kinds=kinds, service=service))
    plan = custodian / "roots.json"
    plan.write_text(json.dumps(dict(format="prod-backup-roots-v1", event_id=event["id"], instance_id=instance_id,
                                   manual_kinds=["snapshot"], volumes=volumes)))
    monkeypatch.setenv("MAISON_PROD_BACKUP_ROOTS", str(plan))
    monkeypatch.setenv("MAISON_PROD_BACKUP_ROOTS_SHA256", hashlib.sha256(plan.read_bytes()).hexdigest())
    return root, config, descriptor, state, recipient


@pytest.fixture
def signed_decision(instance, monkeypatch):
    """A synthetic, offline custodian explicitly authorizes a particular drill."""
    from nacl.signing import SigningKey
    import prod_backup as backup
    import prod_restore_approval as permits
    from prod_custody import APPROVALS, CUTOFF

    root, config, _, _, _ = instance
    signer = SigningKey.generate()
    private = root / "separate-approval-records"
    private.mkdir(mode=0o700)
    anchor = private / "approver.der"
    anchor.write_bytes(permits.ED25519_SPKI_PREFIX + bytes(signer.verify_key))
    anchor.chmod(0o444)
    alias = root / "mac-etc-alias"
    alias.symlink_to(private.parent, target_is_directory=True)
    monkeypatch.setattr(permits, "APPROVER_KEY", alias/private.name/anchor.name)
    monkeypatch.setattr(permits, "TRUST_OWNER", os.geteuid())
    register = backup.local_ledger()
    event = "prod-synthetic-fixture"
    identifier = register.status(event, None, CUTOFF).instance_id
    roots = backup.custody.Roots.from_env(event, identifier)

    def sign(archive, digest, pin, target, *, overrides=None, lifecycle_overrides=None):
        now = register.fixture_time[0]
        archive_id = roots.ident(archive, "archive")
        created = next(item["created_at"] for item in register.objects(event, identifier, CUTOFF)
                       if item["object_id"] == archive_id)
        batch = private / secrets.token_hex(12)
        batch.mkdir(mode=0o700)
        files = permits.ApprovalFiles(*(batch / name for name in ("decision", "sig", "receipt", "lifecycle", "policy")))
        receipt = dict(format="prod-object-receipt-v1", event_id=event, instance_id=identifier, created_at=created,
                       archive_sha256=digest, size=archive.stat().st_size, object_key="copies/" + archive.name,
                       version_id="synthetic-version")
        policy = dict(format="prod-lifecycle-approval-v1", event_id=event, instance_id=identifier,
                      access_until=CUTOFF, policy={**dict.fromkeys(APPROVALS, "c" * 64), "retention_days": 14})
        files.policy.write_text(json.dumps(policy))
        disposition = dict(format="prod-erasure-evidence-v1", event_id=event, instance_id=identifier,
                           cutoff=CUTOFF, observed_at=now, closure=now >= CUTOFF, local_db_purge_verified_at=None,
                           backups_expired=False, inventory_outstanding=1, inventory_unresolved=0,
                           approval_sha256=permits.sha(files.policy.read_bytes()))
        disposition.update(lifecycle_overrides or {})
        files.receipt.write_text(json.dumps(receipt))
        files.lifecycle.write_text(json.dumps(disposition))
        record = dict(format="prod-restore-decision-v1", event_id=event, instance_id=identifier, access_until=CUTOFF,
                      receipt_sha256=permits.sha(files.receipt.read_bytes()), lifecycle_sha256=permits.sha(files.lifecycle.read_bytes()),
                      copy_status="available", disposition="closed" if now >= CUTOFF else "open",
                      register_id=register.fixture_register_id, request_id=secrets.token_hex(16), event_sha256=permits.sha(config.read_bytes()), cert_sha256=pin,
                      destination_id=roots.ident(target, "restore"), issued_at=now, valid_until=now+1800)
        record.update(overrides or {})
        files.decision.write_text(json.dumps(record))
        files.signature.write_bytes(signer.sign(files.decision.read_bytes()).signature)
        return files
    return sign


@pytest.fixture
def approved_restore(signed_decision):
    import prod_backup as backup

    def restore(archive, digest, config, descriptor, cert, pin, identity, target):
        decision = signed_decision(archive, digest, pin, target)
        return backup.restore(archive, digest, config, descriptor, cert, pin, identity, target, approval=decision)
    return restore


def test_online_restore_keeps_cards_qr_and_three_audits_retires_both_roles(instance, recipient_cert, approved_restore):
    import prod
    import prod_backup as backup
    from prod_fixtures import ORIGIN
    from staff_admin import manager_key_path, staff_key_path, write_staff_password

    root, config, descriptor, state, recipient = instance
    cert, pin, identity = recipient_cert
    live = prod.database_path(state)
    old_pass = {role: secrets.token_urlsafe(24) for role in ("staff", "admin")}
    for role in old_pass:
        write_staff_password(live, role, old_pass[role], role=role, production=True)
    old_verifiers = {role: path(live).read_bytes() for role, path in (("staff", staff_key_path), ("admin", manager_key_path))}
    profile = prod.application.default_profile_key_path(live).read_bytes()
    with closing(sqlite3.connect(live)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO customers VALUES ('fixture-card',1,33)")
        writer.execute("INSERT INTO customer_profiles VALUES ('fixture-card','Synthetic','fixture-hmac','fixture-notice',1)")
        recipient.store(writer, "fixture-card", "010" + "0" * 8, 1)
        writer.execute("INSERT INTO stamps VALUES ('fixture-card','fixture-02',2)")
        writer.execute("INSERT INTO reward_audit VALUES ('fixture-card',33,'unauthenticated_customer_screen')")
        writer.execute("INSERT INTO stamp_cancel_audit VALUES (1,'fixture-card','fixture-03',3,4,'staff')")
        correction = (1, "f"*32, "fixture-card", "admin", 4, "replace", "wrong_product", "synthetic test reason",
                      "fixture-01", "fixture-02", 2, 1, 0, 0)
        writer.execute("INSERT INTO stamp_correction_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", correction)
        writer.execute("INSERT INTO contact_access_audit VALUES ('fixture-request',9,'operator','fixture-card','pickup','displayed')")
        writer.execute("INSERT INTO admin_sessions VALUES ('fixture-token','csrf','staff',1,10)")
        writer.execute("INSERT INTO sessions VALUES ('fixture-session','fixture-card','csrf',99)")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO customers VALUES ('not-committed',5,NULL)")
        archive = root / "prod-vault" / "fixture.cms"
        receipt = backup.create(state, config, descriptor, cert, pin, archive)
        writer.rollback()
    assert receipt["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert receipt["evidence"]["counts"] == dict(products=10, customers=1, customer_profiles=1,
        sessions=1, stamps=1, customer_contacts=1, reward_audit=1, stamp_cancel_audit=1,
        stamp_correction_audit=1, contact_access_audit=1)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert b"SQLite format 3" not in archive.read_bytes() and profile not in archive.read_bytes()
    plaintext = subprocess.check_output(["openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
                                        "-recip", cert, "-inkey", identity, "-in", archive])
    with zipfile.ZipFile(io.BytesIO(plaintext)) as payload:
        assert set(payload.namelist()) == {prod.DATABASE_NAME, backup.PROFILE_KEY, backup.MANIFEST}
        assert json.loads(payload.read(backup.MANIFEST)) == receipt["evidence"]
    target = root / "offline" / "candidate"
    assert approved_restore(archive, receipt["archive_sha256"], config, descriptor, cert, pin, identity, target) == receipt
    staged = prod.database_path(target)
    assert {p.name for p in staged.parent.iterdir()} == {prod.DATABASE_NAME, backup.PROFILE_KEY}
    assert prod.application.default_profile_key_path(staged).read_bytes() == profile
    with closing(sqlite3.connect(staged)) as candidate, closing(sqlite3.connect(live)) as original:
        for table in ("customers", "customer_profiles", "customer_contacts", "sessions", "products", "stamps",
                      "stamp_cancel_audit", "reward_audit", "stamp_correction_audit", "contact_access_audit"):
            assert original.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() == candidate.execute(
                "SELECT * FROM " + table + " ORDER BY 1").fetchall()
        assert candidate.execute("SELECT COUNT(*) FROM admin_sessions").fetchone() == (0,)
        assert original.execute("SELECT COUNT(*) FROM admin_sessions").fetchone() == (1,)
    # Copying either old verifier cannot substitute for fresh issuance.
    for role, path in (("staff", staff_key_path), ("admin", manager_key_path)):
        verifier = path(staged)
        verifier.write_bytes(old_verifiers[role])
        verifier.chmod(0o600)
    with pytest.raises(ValueError):
        prod.prod_app(target, config, ORIGIN, recipient, authority=backup.local_ledger())
    fresh = {role: secrets.token_urlsafe(24) for role in ("staff", "admin")}
    for role in fresh:
        write_staff_password(staged, role, fresh[role], role=role, production=True)
    client = prod.prod_app(target, config, ORIGIN, recipient, authority=backup.local_ledger()).test_client()
    for role, route in (("staff", "login"), ("admin", "manager-login")):
        def attempt(password):
            return client.post("/api/admin/" + route, base_url=ORIGIN, json={"password": password},
                               headers={"Origin": ORIGIN, "X-Stamp-Client": "1"})
        assert attempt(old_pass[role]).status_code == 401
        assert attempt(fresh[role]).json["role"] == role


def test_reject_existing_paths_wrong_cert_corrupt_archive_and_aliased_directory(instance, recipient_cert, approved_restore):
    import prod_backup as backup
    root, config, descriptor, state, recipient = instance
    cert, pin, identity = recipient_cert
    vault = root / "prod-vault"
    archive, target = vault / "fixture.cms", root / "offline" / "candidate"
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, "0"*64, archive)
    assert not archive.exists()
    receipt = backup.create(state, config, descriptor, cert, pin, archive)
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, pin, archive)
    with pytest.raises((ValueError, FileExistsError)):
        approved_restore(archive, receipt["archive_sha256"], config, descriptor, cert, pin, identity, state)
    with pytest.raises(ValueError):
        approved_restore(archive, "0"*64, config, descriptor, cert, pin, identity, target)
    assert not target.exists()
    alias = root / "alias"
    alias.symlink_to(vault, target_is_directory=True)
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, pin, alias / "new.cms")
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, pin, state / "new.cms")
    assert not (vault / "new.cms").exists() and not (state / "new.cms").exists()
    damaged = archive.read_bytes()[:-80]
    archive.write_bytes(damaged)
    with pytest.raises(subprocess.CalledProcessError):
        approved_restore(archive, hashlib.sha256(damaged).hexdigest(), config, descriptor, cert, pin, identity, target)
    assert not target.exists()


def test_rejects_wrong_contact_key_and_prod_manifest_changes(instance, recipient_cert, approved_restore):
    import prod_backup as backup
    from nacl.public import PrivateKey

    root, config, descriptor, state, recipient = instance
    cert, pin, identity = recipient_cert
    archive, target = root / "prod-vault" / "fixture.cms", root / "offline" / "candidate"
    receipt = backup.create(state, config, descriptor, cert, pin, archive)
    wrong = json.loads(descriptor.read_text())
    wrong["public_key"] = bytes(PrivateKey.generate().public_key).hex()
    other = root / "wrong-recipient.json"
    other.write_text(json.dumps(wrong))
    with pytest.raises(ValueError):
        approved_restore(archive, receipt["archive_sha256"], config, other, cert, pin, identity, target)
    assert not target.exists()
    # Even a decryptable, authenticated package must pass its embedded evidence and state checks.
    clear = subprocess.check_output(["openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
                                     "-recip", cert, "-inkey", identity, "-in", archive])
    for invalid in ("manifest", "extra-file"):
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(clear)) as source, zipfile.ZipFile(stream, "w") as destination:
            for name in source.namelist():
                content = source.read(name)
                if invalid == "manifest" and name == backup.MANIFEST:
                    record = json.loads(content)
                    record["counts"]["contact_access_audit"] += 1
                    content = json.dumps(record).encode()
                destination.writestr(name, content)
            if invalid == "extra-file":
                destination.writestr("../fixture", "outside")
        data = subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-outform", "DER",
                               "-aes-256-gcm", "-recip", cert], input=stream.getvalue(), capture_output=True, check=True).stdout
        archive.write_bytes(data)
        with pytest.raises(ValueError):
            approved_restore(archive, hashlib.sha256(data).hexdigest(), config, descriptor, cert, pin, identity, target)
        assert not target.exists() and not (root / "fixture").exists()


def test_backup_refuses_cross_environment_state_and_output_inside_git(instance, recipient_cert):
    import prod
    import prod_backup as backup

    root, config, descriptor, state, _ = instance
    cert, pin, _ = recipient_cert
    vault = root / "prod-vault"
    archive = vault / "new.cms"
    (vault / ".git").touch()
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, pin, archive)
    assert not archive.exists()
    (vault / ".git").unlink()
    with closing(sqlite3.connect(prod.database_path(state))) as db, db:
        db.execute("UPDATE metadata SET key='linux_demo_v1' WHERE key='linux_prod_v1'")
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, pin, archive)
    assert not archive.exists()


@pytest.mark.parametrize("interrupt", [False, True])
def test_only_ciphertext_is_written_to_the_vault(instance, recipient_cert, monkeypatch, interrupt):
    import prod_backup as backup

    root, config, descriptor, state, _ = instance
    cert, pin, _ = recipient_cert
    vault = root / "prod-vault"
    target = vault / "new.cms"
    run = subprocess.run
    observed = []

    def observe(command, **kwargs):
        if command[:3] == ["openssl", "cms", "-encrypt"]:
            plain = Path(command[command.index("-in") + 1])
            assert plain.parent.parent == state and plain.parent != state / "instance"
            assert plain.parent.stat().st_mode & 0o777 == 0o700
            assert (plain.parent / backup.PROFILE_KEY).is_file()
            observed.append(Path(kwargs["stdout"].name))
            assert [p for p in vault.iterdir() if p.name != backup.custody.MARKER] == observed and observed[0].suffix == ".cms"
            if interrupt:
                raise subprocess.CalledProcessError(1, ["openssl", "cms"])
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    if interrupt:
        with pytest.raises(subprocess.CalledProcessError):
            backup.create(state, config, descriptor, cert, pin, target)
        assert list(vault.iterdir()) == [vault / backup.custody.MARKER]
    else:
        receipt = backup.create(state, config, descriptor, cert, pin, target)
        assert set(vault.iterdir()) == {target, vault / backup.custody.MARKER}
        assert hashlib.sha256(target.read_bytes()).hexdigest() == receipt["archive_sha256"]
    assert len(observed) == 1 and not observed[0].exists()
    assert list(state.iterdir()) == [state / "instance"]


def test_closed_restore_erases_quarantined_records_without_reporting_live_purge(instance, recipient_cert, approved_restore):
    import prod
    import prod_backup as backup
    from prod_authority import CUTOFF

    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    ledger = backup.local_ledger()
    ctx = ("prod-synthetic-fixture", ledger.status("prod-synthetic-fixture", None, CUTOFF).instance_id, CUTOFF)
    with closing(sqlite3.connect(prod.database_path(state))) as db, db:
        db.execute("INSERT INTO customers VALUES ('synthetic-closure-card',1,NULL)")
        db.execute("INSERT INTO customer_profiles VALUES ('synthetic-closure-card','synthetic','opaque','v1',1)")
        db.execute("INSERT INTO sessions VALUES ('opaque-session','synthetic-closure-card','opaque-csrf',9)")
    archive, target = root / "prod-vault" / "old.cms", root / "offline" / "closed"
    receipt = backup.create(state, config, descriptor, cert, pin, archive)
    ledger.fixture_time[0] = CUTOFF
    with pytest.raises(ValueError):
        backup.create(state, config, descriptor, cert, pin, root / "prod-vault" / "late.cms")
    saved = approved_restore(archive, receipt["archive_sha256"], config, descriptor, cert, pin, identity, target)
    assert saved == {"archive_sha256": receipt["archive_sha256"], "sanitized": True}
    with closing(sqlite3.connect(prod.database_path(target))) as db:
        assert backup.prod_purge.has_local_purge(db)
        assert all(db.execute('SELECT count(*) FROM ' + tbl).fetchone() == (0,)
                   for tbl in backup.prod_purge.CUSTOMER_TABLES)
    assert not ledger.status(*ctx).purged
    with closing(sqlite3.connect(prod.database_path(state))) as db:
        assert db.execute('SELECT count(*) FROM customers').fetchone() == (1,)


def test_disconnected_receipt_prevents_decryption(instance, recipient_cert, monkeypatch, approved_restore):
    import prod_backup as backup
    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    archive, target = root / "prod-vault" / "old.cms", root / "offline" / "unavailable"
    receipt = backup.create(state, config, descriptor, cert, pin, archive)
    ledger = backup.local_ledger()

    def unavailable(*args):
        raise ValueError("offline restore inventory unavailable")

    monkeypatch.setattr(ledger, "status", unavailable)
    run = subprocess.run

    def reject_decryption(command, **kwargs):
        assert command[:3] != ["openssl", "cms", "-decrypt"]
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_decryption)
    with pytest.raises(ValueError, match="offline"):
        approved_restore(archive, receipt["archive_sha256"], config, descriptor, cert, pin, identity, target)
    assert not target.exists()


def test_cutoff_during_restore_sanitizes_before_return(instance, recipient_cert, monkeypatch, approved_restore):
    import prod_backup as backup
    import prod
    from prod_authority import CUTOFF
    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    with closing(sqlite3.connect(prod.database_path(state))) as db, db:
        db.execute("INSERT INTO customers VALUES ('fixture-before-cutoff',1,NULL)")
    backup.local_ledger().fixture_time[0] = CUTOFF - 1
    archive, target = root / "prod-vault" / "before.cms", root / "offline" / "cutover"
    receipt = backup.create(state, config, descriptor, cert, pin, archive)
    ledger = backup.local_ledger()
    original = ledger.check_archive
    times = []

    def check(*args):
        times.append(1)
        if len(times) == 4:
            ledger.fixture_time[0] = CUTOFF
        return original(*args)
    monkeypatch.setattr(ledger, "check_archive", check)
    result = approved_restore(archive, receipt["archive_sha256"], config, descriptor, cert, pin, identity, target)
    assert result.get("sanitized") and len(times) == 4
    with closing(sqlite3.connect(prod.database_path(target))) as db:
        assert db.execute("SELECT count(*) FROM customers").fetchone() == (0,)


def test_expiry_requires_all_registered_volumes_and_receipts_and_flags_reappearance(instance, recipient_cert, approved_restore):
    import prod_backup as backup
    from prod_authority import CUTOFF
    from prod_backup_custody import Roots, expire, MARKER
    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    archive, candidate = root / "prod-vault" / "known.cms", root / "offline" / "candidate"
    ledger = backup.local_ledger()
    event = "prod-synthetic-fixture"
    binding = (event, ledger.status(event, None, CUTOFF).instance_id, CUTOFF)
    plan = Roots.from_env(event, binding[1])
    proofs = root / "independent-receipts"
    proofs.mkdir(mode=0o700)
    receipt = backup.create(state, config, descriptor, cert, pin, archive)
    approved_restore(archive, receipt["archive_sha256"], config, descriptor, cert, pin, identity, candidate)
    unknown = root / "offline" / "unregistered-snapshot"
    unknown.write_text("synthetic-disposable")
    unknown.chmod(0o600)
    ledger.fixture_time[0] = CUTOFF
    with pytest.raises(ValueError, match="Unregistered"):
        expire(plan, ledger, *binding, proofs)
    assert archive.exists() and candidate.exists() and not list(proofs.iterdir())
    obj = plan.ident(unknown, "snapshot")
    ledger.discover(*binding, "snapshot", obj)
    marker = archive.parent / MARKER
    marker.rename(root / "missing-volume-marker")
    with pytest.raises((ValueError, OSError)):
        expire(plan, ledger, *binding, proofs)
    assert archive.exists() and candidate.exists()
    (root / "missing-volume-marker").rename(marker)
    result = expire(plan, ledger, *binding, proofs)
    assert result["verified_absent"] == 5 and not (archive.exists() or unknown.exists() or candidate.exists())
    assert dict((row["object_id"], row["deleted_at"]) for row in ledger.objects(*binding))[obj] == CUTOFF
    receipts = {hashlib.sha256(f.read_bytes()).hexdigest() for f in proofs.iterdir()}
    assert all(row["receipt_sha256"] in receipts for row in ledger.objects(*binding))
    assert not ledger.status(*binding).backups_expired and "snapshot" not in result["reconciled"]
    # Simulated *independent* provider control-plane proof, supplied only after its own listing.
    provider_digest = hashlib.sha256(b"synthetic-provider-version-evidence").hexdigest()
    ledger.reconcile(*binding, "snapshot", provider_digest)
    assert ledger.status(*binding).backups_expired
    unknown.write_text("simulated-reappeared-copy")
    with pytest.raises(ValueError, match="reappeared"):
        expire(plan, ledger, *binding, proofs)
    assert not ledger.status(*binding).backups_expired


def test_daily_retention_expiry_before_event_cutoff(instance, recipient_cert, approved_restore):
    import prod_backup as backup
    from prod_authority import CUTOFF
    from prod_backup_custody import Roots, expire
    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    ledger = backup.local_ledger()
    event = "prod-synthetic-fixture"
    binding = (event, ledger.status(event, None, CUTOFF).instance_id, CUTOFF)
    archive = root / "prod-vault" / "daily.cms"
    record = backup.create(state, config, descriptor, cert, pin, archive)
    proofs = root / "receipts"
    proofs.mkdir(mode=0o700)
    plan = Roots.from_env(event, binding[1])
    ledger.fixture_time[0] += 14 * 86400 - 1
    assert expire(plan, ledger, *binding, proofs)["verified_absent"] == 0 and archive.exists()
    ledger.fixture_time[0] += 1
    with pytest.raises(ValueError):
        approved_restore(archive, record["archive_sha256"], config, descriptor, cert, pin, identity,
                       root / "offline" / "expired")
    result = expire(plan, ledger, *binding, proofs)
    assert result == {"verified_absent": 3, "reconciled": []} and not archive.exists()
    assert not ledger.status(*binding).closed and not ledger.status(*binding).backups_expired


def test_local_closure_latch_prevents_backup_after_clock_rollback(instance, recipient_cert):
    import prod_backup as backup
    root, config, descriptor, state, _ = instance
    cert, pin, _ = recipient_cert
    (state / ".privacy-closed").mkdir(mode=0o700)
    archive = root / "prod-vault" / "backdated.cms"
    with pytest.raises(ValueError, match="closed"):
        backup.create(state, config, descriptor, cert, pin, archive)
    assert not archive.exists()


def test_posix_absence_cannot_attest_provider_snapshot_on_archive_volume(instance):
    import prod_backup as backup
    from prod_authority import CUTOFF
    from prod_backup_custody import Roots, expire, location_id

    root, _, _, _, _ = instance
    event = "prod-synthetic-fixture"
    ledger = backup.local_ledger()
    binding = (event, ledger.status(event, None, CUTOFF).instance_id, CUTOFF)
    plan = Roots.from_env(event, binding[1])
    archive_volume = plan.volumes[root / "prod-vault"]["volume_id"]
    provider_object = location_id(archive_volume, "provider-version-absent-from-mount")
    ledger.discover(*binding, "snapshot", provider_object)
    ledger.fixture_time[0] = CUTOFF
    receipts = root / "receipts"
    receipts.mkdir(mode=0o700)
    result = expire(plan, ledger, *binding, receipts)
    assert result["verified_absent"] == 0 and "snapshot" not in result["reconciled"]
    assert ledger.objects(*binding)[0]["deleted_at"] is None
    assert not ledger.status(*binding).backups_expired


def test_fixed_cutoff_disposes_existing_copy_without_daily_retention_approval(instance):
    from prod_authority import APPROVALS, CUTOFF, CustodyLedger
    from prod_backup import local_ledger
    from prod_backup_custody import Roots, expire
    root, _, _, _, _ = instance
    event = "prod-synthetic-fixture"
    binding = (event, local_ledger().status(event, None, CUTOFF).instance_id, CUTOFF)
    policy = {**dict.fromkeys(APPROVALS, "e" * 64), "retention_approval": None, "retention_days": None}
    clock = [CUTOFF - 1]
    directory = root / "distinct-custodian"
    directory.mkdir(mode=0o700)
    ledger = CustodyLedger.create(directory / "ledger.sqlite", *binding, policy, clock=lambda: clock[0])
    plan = Roots.from_env(event, binding[1])
    copy = root / "prod-vault" / "preexisting.cms"
    copy.write_bytes(b"synthetic pre-existing backup")
    copy.chmod(0o600)
    ledger.discover(*binding, "archive", plan.ident(copy, "archive"))
    proofs = root / "receipts"
    proofs.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="daily retention"):
        expire(plan, ledger, *binding, proofs)
    ledger.close(*binding)
    with pytest.raises(ValueError, match="deadline"):
        expire(plan, ledger, *binding, proofs)
    assert copy.exists() and not list(proofs.iterdir())

    clock[0] = CUTOFF
    result = expire(plan, ledger, *binding, proofs)
    assert result["verified_absent"] == 1 and not copy.exists()
    assert ledger.objects(*binding)[0]["deleted_at"] == CUTOFF
    assert not ledger.status(*binding).backup_ready


def test_missing_changed_self_signed_or_stale_approval_stops_before_decryption(
        instance, recipient_cert, signed_decision, monkeypatch):
    from nacl.signing import SigningKey
    import prod_backup as backup
    from prod_custody import CUTOFF

    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    archive, target = root/"prod-vault"/"fixture.cms", root/"offline"/"candidate"
    record = backup.create(state, config, descriptor, cert, pin, archive)
    checksum = record["archive_sha256"]
    args = (archive, checksum, config, descriptor, cert, pin, identity, target)
    now = backup.local_ledger().fixture_time[0]
    original = subprocess.run

    def no_decryption(command, **kwargs):
        assert command[:3] != ["openssl", "cms", "-decrypt"]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_decryption)
    cases = [
        None,
        signed_decision(archive, checksum, pin, target, overrides={"instance_id": "0"*64}),
        signed_decision(archive, checksum, pin, target, overrides={"event_sha256": "0"*64}),
        signed_decision(archive, checksum, pin, target, overrides={"destination_id": "0"*64}),
        signed_decision(archive, checksum, pin, target, overrides={"issued_at": now-1801, "valid_until": now}),
        signed_decision(archive, checksum, pin, target, overrides={"copy_status": "retired"}),
        signed_decision(archive, checksum, pin, target, lifecycle_overrides={"backups_expired": True}),
    ]
    altered = signed_decision(archive, checksum, pin, target)
    altered.receipt.write_bytes(altered.receipt.read_bytes() + b"\n")
    cases.append(altered)
    foreign = signed_decision(archive, checksum, pin, target)
    foreign.signature.write_bytes(SigningKey.generate().sign(foreign.decision.read_bytes()).signature)
    cases.append(foreign)
    missing = signed_decision(archive, checksum, pin, target)
    missing.lifecycle.unlink()
    cases.append(missing)
    for decision in cases:
        with pytest.raises((ValueError, FileNotFoundError)):
            backup.restore(*args, approval=decision)
        assert not target.exists()

    backup.local_ledger().fixture_time[0] = CUTOFF
    for overrides, evidence in [
        ({"disposition": "purged"}, {}),  # No actual local purge receipt timestamp
        ({}, {"local_db_purge_verified_at": CUTOFF + 1}),
        ({"disposition": "open"}, {"closure": False}),
        ({}, {"observed_at": CUTOFF-1}),
        ({}, {"inventory_unresolved": 1}),
    ]:
        decision = signed_decision(archive, checksum, pin, target, overrides=overrides, lifecycle_overrides=evidence)
        with pytest.raises(ValueError):
            backup.restore(*args, approval=decision)
        assert not target.exists()


def test_consumed_approval_cannot_be_replayed_after_failed_decryption(
        instance, recipient_cert, signed_decision, monkeypatch):
    import prod_backup as backup
    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    archive, target = root/"prod-vault"/"fixture.cms", root/"offline"/"candidate"
    checksum = backup.create(state, config, descriptor, cert, pin, archive)["archive_sha256"]
    args = (archive, checksum, config, descriptor, cert, pin, identity, target)
    decision = signed_decision(archive, checksum, pin, target)
    original = subprocess.run
    decrypts = []

    def interrupt(command, **kwargs):
        if command[:3] == ["openssl", "cms", "-decrypt"]:
            decrypts.append(True)
            raise subprocess.CalledProcessError(1, command)
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", interrupt)
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(*args, approval=decision)
    assert not target.exists()
    with pytest.raises(ValueError, match="already been used"):
        backup.restore(*args, approval=decision)
    assert len(decrypts) == 1 and not target.exists()


def test_independent_offline_recovery_with_empty_workstation_inventory(
        instance, recipient_cert, signed_decision, monkeypatch):
    import prod_backup as backup
    import prod_restore_approval as permits
    from prod_custody import CustodyLedger

    root, config, descriptor, state, _ = instance
    cert, pin, identity = recipient_cert
    archive, target = root/"prod-vault"/"fixture.cms", root/"offline"/"candidate"
    checksum = backup.create(state, config, descriptor, cert, pin, archive)["archive_sha256"]
    original_creation = backup.local_ledger().fixture_time[0]
    backup.local_ledger().fixture_time[0] += 7*86400
    decision = signed_decision(archive, checksum, pin, target)
    policy = json.loads(decision.policy.read_bytes())
    binding = (policy["event_id"], policy["instance_id"], policy["access_until"])
    register_dir = root/"isolated-custody"
    register_dir.mkdir(mode=0o700)
    inventory_path = register_dir/"inventory.sqlite"
    with pytest.raises(ValueError):
        permits.init_register(inventory_path, decision.policy, "0"*64)
    assert not inventory_path.exists()
    register_id = permits.init_register(inventory_path, decision.policy, permits.sha(decision.policy.read_bytes()))
    with pytest.raises(FileExistsError):
        permits.init_register(inventory_path, decision.policy, permits.sha(decision.policy.read_bytes()))
    now = backup.local_ledger().fixture_time
    register = CustodyLedger(inventory_path, clock=lambda: now[0])
    monkeypatch.setattr(permits, "CustodyLedger", lambda path: CustodyLedger(path, clock=lambda: now[0]))
    assert not register.objects(*binding)
    monkeypatch.setenv("MAISON_PROD_RESTORE_REGISTER", str(register.path))
    monkeypatch.setattr(backup, "recovery_inventory", permits.recovery_inventory)
    with pytest.raises(ValueError, match="another workstation"):
        backup.restore(archive, checksum, config, descriptor, cert, pin, identity, target, approval=decision)
    decision = signed_decision(archive, checksum, pin, target, overrides={"register_id": register_id})
    def unavailable():
        raise AssertionError("restore attempted to access the lost service host")
    monkeypatch.setattr(backup, "local_ledger", unavailable)
    result = backup.restore(archive, checksum, config, descriptor, cert, pin, identity, target, approval=decision)
    assert result["archive_sha256"] == checksum
    copies = register.objects(*binding)
    assert {row["kind"] for row in copies} == {"archive", "restore"}
    assert all(row["created_at"] == original_creation for row in copies)
    roots_path = Path(os.environ["MAISON_PROD_BACKUP_ROOTS"])
    mac_plan = json.loads(roots_path.read_bytes())
    mac_plan["volumes"] = [row for row in mac_plan["volumes"] if not row["service"]]
    plan_file = register_dir/"mac-volumes.json"
    plan_file.write_text(json.dumps(mac_plan))
    monkeypatch.setenv("MAISON_PROD_BACKUP_ROOTS", str(plan_file))
    monkeypatch.setenv("MAISON_PROD_BACKUP_ROOTS_SHA256", permits.sha(plan_file.read_bytes()))
    proofs = root/"recovery-expiry-receipts"
    proofs.mkdir(mode=0o700)
    monkeypatch.setattr("sys.argv", ["prod_restore_approval.py", "expire-register", "--config", str(config),
                                      "--instance", binding[1], "--receipts", str(proofs)])
    now[0] = original_creation + 14*86400
    permits.main()
    assert not archive.exists() and not target.exists() and len(list(proofs.iterdir())) == 2
    assert (state/"instance"/"prod.sqlite").is_file()
