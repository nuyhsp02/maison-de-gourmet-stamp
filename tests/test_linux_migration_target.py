"""Public migration gating with isolated, empty synthetic pairs only."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/linux"))
import demo
import migration_target as guard

ORIGIN = "https://migration.example.invalid"
EVENT_ID = "demo-migration-guard"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux file identity and permissions")


@pytest.fixture
def drill(tmp_path):
    event = json.loads((ROOT / "config/event.example.json").read_text())
    event.update(id=EVENT_ID, draft=False, starts_at="2026-12-01T00:00:00+09:00",
                 ends_at="2027-02-01T00:00:00+09:00", reward_until="2027-03-01T00:00:00+09:00")
    config = tmp_path / "event.json"
    config.write_text(json.dumps(event), encoding="utf-8")
    pair = demo.prepare_migration(tmp_path.resolve() / "drill", config, ORIGIN, purpose="corrections")
    return pair, config


def attempt(pair, config, **changes):
    options = dict(state_dir=pair.target, config=config, origin=ORIGIN, source_state_dir=pair.source,
                   expected_event_id=EVENT_ID, expected_schema="base")
    demo.migrate_corrections(**{**options, **changes})


def inventory(state):
    with closing(demo.connect_db(state / demo.DATABASE_NAME, mode="ro")) as db:
        return (dict(db.execute("SELECT * FROM metadata")), demo.contact.schema_rows(db),
                dict(db.execute("SELECT id,qr_key FROM products")), db.execute("SELECT COUNT(*) FROM customers").fetchone()[0])


def test_public_cli_migrates_only_confirmed_target(drill):
    pair, config = drill
    before = inventory(pair.source)
    assert inventory(pair.target) == before
    result = subprocess.run([sys.executable, "-B", demo.__file__, "migrate-corrections",
                             "--state-dir", str(pair.target), "--source-state-dir", str(pair.source),
                             "--expected-event-id", EVENT_ID, "--expected-schema", "base",
                             "--config", str(config), "--origin", ORIGIN], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert inventory(pair.source) == before
    after = inventory(pair.target)
    assert after[0]["correction_schema_v1"] == "1" and after[2:] == before[2:]
    attempt(pair, config, expected_schema="corrections")
    assert inventory(pair.target) == after


def test_receipt_needed_before_reading_any_database(drill, monkeypatch):
    pair, config = drill
    monkeypatch.setattr(demo, "connect_db", lambda *args, **kwargs: pytest.fail("opened an unconfirmed DB"))
    for missing in ({"source_state_dir": None}, {"expected_event_id": None}, {"expected_schema": None}):
        with pytest.raises(guard.MigrationError, match="^migration_confirmation_missing$"):
            attempt(pair, config, **missing)
    with pytest.raises(guard.MigrationError, match="^migration_same_database$"):
        attempt(pair, config, source_state_dir=pair.target)
    with pytest.raises(guard.MigrationError, match="^migration_event_id$"):
        attempt(pair, config, expected_event_id="demo-unrelated")
    with pytest.raises(guard.MigrationError, match="^migration_purpose$"):
        demo.migrate_contacts(pair.target, config, ORIGIN, config, source_state_dir=pair.source,
                              expected_event_id=EVENT_ID, expected_schema="base")
    pair.receipt.unlink()
    with pytest.raises(guard.MigrationError, match="^migration_confirmation_missing$"):
        attempt(pair, config)


@pytest.mark.parametrize(("change", "code"), [
    ("event_id", "migration_event_id"), ("event_signature", "migration_event_signature"),
    ("products", "migration_product_set"), ("qr", "migration_qr_set"), ("customers", "migration_has_records"),
    ("stamps", "migration_has_records"), ("schema", "migration_schema"), ("schema_marker", "migration_schema"),
    ("signature", "migration_source_inventory"),
])
def test_invalid_target_rejected_before_writable_open(drill, monkeypatch, change, code):
    pair, config = drill
    options = {}
    if change == "event_id":
        event = json.loads(config.read_text())
        event["id"] = "demo-wrong-event"
        config.write_text(json.dumps(event))
    elif change == "schema":
        options["expected_schema"] = "contacts"
    else:
        with closing(demo.connect_db(pair.target / demo.DATABASE_NAME, mode="rw")) as db:
            if change == "event_signature":
                db.execute("UPDATE metadata SET value='wrong' WHERE key='event'")
            elif change == "products":
                db.execute("DELETE FROM products WHERE id='season-01'")
            elif change == "qr":
                db.execute("UPDATE products SET qr_key=lower(hex(randomblob(16))) WHERE id='season-01'")
            elif change == "schema_marker":
                db.execute("UPDATE metadata SET value='wrong' WHERE key='admin_schema_v1'")
            elif change == "signature":
                db.execute("UPDATE metadata SET value='wrong' WHERE key='linux_demo_inventory_v1'")
            else:
                db.execute("INSERT INTO customers VALUES ('synthetic',1,NULL)")
                if change == "stamps":
                    db.execute("INSERT INTO stamps VALUES ('synthetic','season-01',1)")
    original, before = inventory(pair.source), inventory(pair.target)
    connect = demo.connect_db
    def require_readonly(path, *, mode):
        assert mode == "ro"
        return connect(path, mode=mode)
    monkeypatch.setattr(demo, "connect_db", require_readonly)
    with pytest.raises(guard.MigrationError, match="^" + code + "$"):
        attempt(pair, config, **options)
    assert inventory(pair.source) == original and inventory(pair.target) == before


@pytest.mark.parametrize("fault", ["live", "live_ancestor", "parent_symlink", "db_symlink",
                                   "key_symlink", "hardlink", "replacement", "relative", "sidecar"])
def test_path_confusion_refused_without_opening_sqlite(drill, monkeypatch, fault):
    pair, config = drill
    target = pair.target
    dbpath = target / demo.DATABASE_NAME
    if fault.startswith("live"):
        monkeypatch.setattr(guard, "LIVE_STATE", target if fault == "live" else target / "service")
    elif fault == "parent_symlink":
        alias = target.parent.parent / "alias"
        alias.symlink_to(target.parent)
        target = alias / target.name
    elif fault in ("db_symlink", "hardlink"):
        dbpath.unlink()
        if fault == "db_symlink":
            dbpath.symlink_to(pair.source / demo.DATABASE_NAME)
        else:
            os.link(pair.source / demo.DATABASE_NAME, dbpath)
    elif fault == "key_symlink":
        key = demo.default_profile_key_path(dbpath)
        key.unlink()
        key.symlink_to(demo.default_profile_key_path(pair.source / demo.DATABASE_NAME))
    elif fault == "replacement":
        moved = target / "moved.sqlite"
        dbpath.rename(moved)
        dbpath.write_bytes(moved.read_bytes())
        dbpath.chmod(0o600)
    elif fault == "relative":
        target = Path("target")
    else:
        sidecar = Path(str(dbpath) + "-wal")
        sidecar.unlink(missing_ok=True)
        sidecar.symlink_to(pair.source / demo.DATABASE_NAME)
    monkeypatch.setattr(demo, "connect_db", lambda *args, **kwargs: pytest.fail("opened a misidentified DB"))
    with pytest.raises(guard.MigrationError):
        attempt(pair, config, state_dir=target)


def test_population_between_preflight_and_write_lock_rejected(drill, monkeypatch):
    pair, config = drill
    connect = demo.connect_db
    def concurrent_connection(path, *, mode):
        if path == pair.target / demo.DATABASE_NAME and mode == "rw":
            with closing(connect(path, mode="rw")) as competitor:
                competitor.execute("INSERT INTO customers VALUES ('during-preflight',1,NULL)")
        return connect(path, mode=mode)
    monkeypatch.setattr(demo, "connect_db", concurrent_connection)
    with pytest.raises(guard.MigrationError, match="migration_has_records"):
        attempt(pair, config)
    after = inventory(pair.target)
    assert after[3] == 1 and "correction_schema_v1" not in after[0]


def test_zero_byte_target_and_reused_workspace_rejected(drill):
    pair, config = drill
    with pytest.raises(guard.MigrationError, match="migration_workspace_exists"):
        demo.prepare_migration(pair.source.parent, config, ORIGIN, purpose="corrections")
    db = pair.target / demo.DATABASE_NAME
    db.write_bytes(b"")
    with pytest.raises(guard.MigrationError, match="migration_invalid_inventory"):
        attempt(pair, config)
    assert db.stat().st_size == 0


@pytest.mark.parametrize("contacts_first", [False, True], ids=["corrections-first", "contacts-first"])
def test_public_drill_accepts_only_explicit_current_schema_at_each_step(drill, tmp_path, contacts_first):
    from nacl.public import PrivateKey

    _, config = drill
    pair = demo.prepare_migration(tmp_path.resolve() / "two-extensions", config, ORIGIN, purpose="contacts+corrections")
    policy = tmp_path / "contact-policy.json"
    policy.write_text(json.dumps({"format": demo.contact.NOTICE, "event_id": EVENT_ID,
                                 "public_key": bytes(PrivateKey.generate().public_key).hex(), "allowed_profiles": []}))
    def contacts(expected):
        demo.migrate_contacts(pair.target, config, ORIGIN, policy, source_state_dir=pair.source,
                              expected_event_id=EVENT_ID, expected_schema=expected)
    before = inventory(pair.source)
    if contacts_first:
        contacts("base")
        attempt(pair, config, expected_schema="contacts")
    else:
        attempt(pair, config)
        contacts("corrections")
    with pytest.raises(guard.MigrationError, match="migration_schema"):
        attempt(pair, config, expected_schema="corrections")
    contacts("contacts+corrections")
    attempt(pair, config, expected_schema="contacts+corrections")
    assert inventory(pair.source) == before
    assert inventory(pair.target)[2:] == before[2:]
