"""Local erasure uses only temporary synthetic production-format SQLite files."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from prod_fixtures import ORIGIN, make_config, prod
import prod_purge


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private Linux PRODUCTION filesystem")
CUTOFF = int(datetime(2027, 3, 30, 15, tzinfo=timezone.utc).timestamp())
CANARY = "SYNTHETIC-ERASURE-FIXTURE-CANARY"


class DrillAuthority:
    """Inject a trusted-state response; transport and anti-replay are tested independently."""
    def __init__(self, instance):
        self.instance = instance
        self.clock = CUTOFF
        self.approved = True
        self.closed = False

    def status(self, event_id, instance, cutoff):
        assert (event_id, cutoff) == ("prod-synthetic-fixture", CUTOFF)
        self.closed |= self.clock >= cutoff
        return SimpleNamespace(instance_id=self.instance, closed=self.closed,
                               purge_authorized=self.approved and self.closed)

    close = status


@pytest.fixture
def instance(tmp_path, monkeypatch):
    directory = tmp_path.resolve()
    config, descriptor = make_config(directory)
    event = prod.load_prod_event(config)
    recipient = prod.contact.ProductionEnrollment.read(descriptor, event["id"])
    state = directory / "synthetic-state"
    state.mkdir(mode=0o700)
    database = prod.prepare(state, config, ORIGIN, recipient)
    with closing(prod.application.connect_db(database, mode="rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO customers VALUES (?,1,3)", (CANARY,))
        db.execute("INSERT INTO customer_profiles VALUES (?,?,?,'test-notice',1)", (CANARY, CANARY, CANARY))
        recipient.store(db, CANARY, "010" + "0" * 8, 1)
        db.execute("INSERT INTO sessions VALUES ('test-session',?,'test-csrf',3)", (CANARY,))
        db.executemany("INSERT INTO admin_sessions VALUES (?,?,?,1,3)",
                       [(role + ":test", "test-csrf", role) for role in ("staff", "manager")])
        db.execute("INSERT INTO stamps VALUES (?,'fixture-01',2)", (CANARY,))
        db.execute("INSERT INTO reward_audit VALUES (?,3,'unauthenticated_customer_screen')", (CANARY,))
        db.execute("INSERT INTO stamp_cancel_audit VALUES (1,?,'fixture-02',2,3,'test-staff')", (CANARY,))
        db.execute("INSERT INTO stamp_correction_audit VALUES (1,?,?,'test-manager',3,'remove',"
                   "'erroneous_stamp',?,'fixture-03',NULL,2,1,0,0)", ("f"*32, CANARY, CANARY))
        # No FK: include a request concerning a card that never existed.
        db.execute("INSERT INTO contact_access_audit VALUES ('test-rid',2,'test-operator',?,'inquiry','unavailable')",
                   (CANARY + "-unmatched",))
        db.execute("ANALYZE")
        authority = DrillAuthority(prod.staff_admin.prod_instance(db))
    monkeypatch.setattr(prod_purge.time, "time", lambda: CUTOFF)
    return state, event, recipient, authority


def state_of(database):
    with closing(prod.application.connect_db(database, mode="ro")) as db:
        return (tuple(db.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in prod_purge.CUSTOMER_TABLES),
                prod_purge.has_local_purge(db), prod.contact.schema_rows(db),
                list(map(tuple, db.execute("SELECT * FROM products ORDER BY id"))))


def test_erasure_removes_redeemed_card_all_linkages_sessions_and_sqlite_remnants(instance):
    state, event, recipient, authority = instance
    database = prod.database_path(state)
    before = state_of(database)
    assert all(before[0]) and not before[1]
    assert CANARY.encode() in database.read_bytes()
    assert set(prod_purge.CUSTOMER_TABLES) == {
        "customers", "customer_profiles", "customer_contacts", "sessions", "admin_sessions", "stamps",
        "reward_audit", "stamp_cancel_audit", "stamp_correction_audit", "contact_access_audit",
    }
    evidence = prod_purge.purge(state, event, recipient, authority)
    after = state_of(database)
    assert not any(after[0]) and after[1] and before[2:] == after[2:]
    assert CANARY not in json.dumps(evidence)
    with closing(prod.application.connect_db(database, mode="rw")) as db:
        assert not db.execute("SELECT * FROM sqlite_stat1").fetchall()
        assert not db.execute("PRAGMA foreign_key_check").fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for suffix in ("", "-wal", "-journal", "-shm"):
        path = Path(str(database) + suffix)
        if path.exists():
            assert CANARY.encode() not in path.read_bytes()
    assert evidence == prod_purge.purge(state, event, recipient, authority)
    assert state_of(database) == after


@pytest.mark.parametrize("issue", ["too-early", "no-disposition-or-drill", "wrong-authority-instance",
                                  "wrong-key", "unknown-meta", "rogue-table", "linked-path", "previous-copy"])
def test_unauthorized_or_unclassified_target_remains_untouched(instance, monkeypatch, issue):
    state, event, recipient, authority = instance
    database = prod.database_path(state)
    if issue == "too-early":
        authority.clock = CUTOFF - 1
        monkeypatch.setattr(prod_purge.time, "time", lambda: CUTOFF - 1)
    elif issue == "no-disposition-or-drill":
        authority.approved = False
    elif issue == "wrong-authority-instance":
        authority.instance = "different"
    elif issue == "wrong-key":
        prod.application.default_profile_key_path(database).write_bytes(b"X" * 32)
    elif issue == "linked-path":
        link = state.with_name("link")
        link.symlink_to(state, target_is_directory=True)
        state = link
    elif issue == "previous-copy":
        (database.parent / "old.snapshot").touch(mode=0o600)
    else:
        with closing(prod.application.connect_db(database, mode="rw")) as db:
            if issue == "unknown-meta":
                db.execute("INSERT INTO metadata VALUES ('unclassified',?)", (CANARY,))
            else:
                db.execute("CREATE TABLE unclassified(data)")
    before = state_of(database)
    with pytest.raises(ValueError):
        prod_purge.purge(state, event, recipient, authority)
    assert state_of(database) == before and not before[1]


def test_delete_error_rolls_back_rows_ddl_and_local_marker(instance, monkeypatch):
    state, event, recipient, authority = instance
    database = prod.database_path(state)
    before = state_of(database)
    original = prod.application.connect_db

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        if kwargs.get("mode") == "rw":
            db.set_authorizer(lambda opcode, arg1, *_: sqlite3.SQLITE_DENY
                              if opcode == sqlite3.SQLITE_DELETE and arg1 == "stamps" else sqlite3.SQLITE_OK)
        return db

    monkeypatch.setattr(prod.application, "connect_db", connect)
    with pytest.raises(sqlite3.DatabaseError):
        prod_purge.purge(state, event, recipient, authority)
    assert state_of(database) == before and not before[1]


def test_physical_failure_is_not_completion_and_retry_finishes(instance, monkeypatch):
    state, event, recipient, authority = instance
    database = prod.database_path(state)

    def fault(db):
        raise sqlite3.OperationalError("synthetic checkpoint failure")

    with monkeypatch.context() as m:
        m.setattr(prod_purge, "compact", fault)
        with pytest.raises(sqlite3.OperationalError):
            prod_purge.purge(state, event, recipient, authority)
    counts, closed, *_ = state_of(database)
    assert closed and not any(counts)
    assert prod_purge.purge(state, event, recipient, authority)["wal_truncated"]


def test_reader_snapshot_withholds_receipt_until_checkpoint_can_truncate(instance, monkeypatch):
    state, event, recipient, authority = instance
    database = prod.database_path(state)
    original = prod.application.connect_db

    def short_timeout(*args, **kwargs):
        db = original(*args, **kwargs)
        db.execute("PRAGMA busy_timeout=100")
        return db

    monkeypatch.setattr(prod.application, "connect_db", short_timeout)
    with closing(original(database, mode="ro")) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT id FROM customers").fetchone()[0] == CANARY
        with pytest.raises(prod.ProdError, match="WAL"):
            prod_purge.purge(state, event, recipient, authority)
    assert prod_purge.purge(state, event, recipient, authority)["customer_tables_empty"]


def test_simultaneous_local_purge_is_rejected_without_changing_the_db(instance):
    import fcntl
    state, event, recipient, authority = instance
    database = prod.database_path(state)
    before = state_of(database)
    fd = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(BlockingIOError):
            prod_purge.purge(state, event, recipient, authority)
        assert state_of(database) == before
    finally:
        os.close(fd)
    assert prod_purge.purge(state, event, recipient, authority)["customer_tables_empty"]


def test_unclassified_rollback_journal_is_preserved_for_inventory(instance):
    state, event, recipient, authority = instance
    database = prod.database_path(state)
    original = database.read_bytes()
    journal = Path(str(database) + "-journal")
    journal.touch(mode=0o600)
    journal.write_bytes(b"synthetic previous journal")
    with pytest.raises(prod.ProdError, match="rollback journal"):
        prod_purge.purge(state, event, recipient, authority)
    # Opening a SQLite handle itself can trigger recovery: compare the backing files directly.
    assert database.read_bytes() == original
    assert journal.read_bytes() == b"synthetic previous journal"
