"""Fresh, synthetic Linux production state. No network, real credentials or real QR output."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Barrier
from xml.etree import ElementTree

import pytest

from lifecycle_fixtures import authority_for, before_privacy_cutoff

from prod_fixtures import ORIGIN, make_config, prod, ready_state
import demo

pytestmark = [pytest.mark.usefixtures("before_privacy_cutoff"),
              pytest.mark.skipif(os.name != "posix", reason="Production runs on a POSIX Linux host")]


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path.resolve()
    config, descriptor = make_config(root)
    recipient = prod.contact.ProductionEnrollment.read(descriptor, "prod-synthetic-fixture")
    state = root / "prod state"
    state.mkdir(mode=0o700)
    return state, config, recipient


def test_preparation_is_new_empty_and_never_reuses_identifiers(inputs):
    state, config, recipient = inputs
    second = state.with_name("separate-state")
    second.mkdir(mode=0o700)
    databases = [prod.prepare(d, config, ORIGIN, recipient) for d in (state, second)]
    ids, qr_sets, keys = [], [], []
    for database in databases:
        key = prod.validate_database(database, prod.load_prod_event(config), recipient)
        keys.append(key)
        with closing(prod.application.connect_db(database, mode="ro")) as db:
            meta = dict(db.execute("SELECT * FROM metadata"))
            assert meta["linux_prod_v1"] == "1" and meta["contact_recipient_v1"] == recipient.key_id
            assert meta["correction_schema_v1"] == "1" and len(meta["prod_instance_id"]) == 64
            ids.append(meta["prod_instance_id"])
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery'").fetchone()
            assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 0
            qr_sets.append({row[0] for row in db.execute("SELECT qr_key FROM products")})
    assert ids[0] != ids[1] and keys[0] != keys[1]
    assert all(len(s) == 10 for s in qr_sets) and qr_sets[0].isdisjoint(qr_sets[1])
    with pytest.raises(prod.ProdError):
        prod.prepare(state, config, ORIGIN, recipient)
    assert prod.validate_database(databases[0], prod.load_prod_event(config), recipient) == keys[0]


def test_two_prepare_calls_publish_one_complete_instance(inputs):
    state, config, recipient = inputs
    start = Barrier(2)
    def attempt():
        start.wait(timeout=5)
        try:
            prod.prepare(state, config, ORIGIN, recipient)
            return True
        except (BlockingIOError, prod.ProdError):
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        a, b = [workers.submit(attempt) for _ in range(2)]
        assert sorted((a.result(), b.result())) == [False, True]
    assert {p.name for p in state.iterdir()} == {"instance"}
    prod.validate_database(prod.database_path(state), prod.load_prod_event(config), recipient)


def test_failed_prepare_never_publishes_partial_state(inputs, monkeypatch):
    state, config, recipient = inputs
    def reject(*_args, **_kwargs):
        raise prod.ProdError("Injected final check failure")
    monkeypatch.setattr(prod, "validate_database", reject)
    with pytest.raises(prod.ProdError):
        prod.prepare(state, config, ORIGIN, recipient)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("key,value", [
    ("draft", True), ("environment", "demo"), ("id", "demo-synthetic-fixture"),
    ("ends_at", "2027-02-01T00:00:00Z"), ("reward_until", "2027-03-01T00:00:00"),
    ("access_until", "2027-03-31T00:00:00.2+09:00"), ("reward", " "),
    ("public_origin", "https://demo.example.invalid"),
])
def test_invalid_catalogue_or_schedule_omits_all_output(inputs, key, value):
    state, config, recipient = inputs
    doc = json.loads(config.read_text())
    doc[key] = value
    config.write_text(json.dumps(doc))
    with pytest.raises(prod.ProdError):
        prod.prepare(state, config, ORIGIN, recipient)
    assert not list(state.iterdir())


@pytest.mark.parametrize("field,value", [("category", "select"), ("id","duplicate"), ("short_name", ""),
                        ("description", None), ("qr_product_id", "different"), ("image", "portfolio.png")])
def test_invalid_product_is_refused(inputs, field, value):
    state, config, recipient = inputs
    doc = json.loads(config.read_text())
    doc["products"][0][field] = doc["products"][1]["id"] if field == "id" else value
    config.write_text(json.dumps(doc))
    with pytest.raises(prod.ProdError):
        prod.prepare(state, config, ORIGIN, recipient)


def test_demonstration_state_cannot_be_cross_opened_or_migrated(inputs):
    state, config, recipient = inputs
    prod_event = prod.load_prod_event(config)
    dbp = prod.prepare(state, config, ORIGIN, recipient)
    droot = state.with_name("demonstration")
    droot.mkdir(mode=0o700)
    demo_path = state.with_name("demo-fixture.json")
    demo_event = json.loads((prod.APP_DIR.parent / "config/event.example.json").read_text())
    demo_event.update(id="demo-other-test", draft=False,
                      **{field: prod_event[field] for field in ("starts_at", "ends_at", "reward_until")})
    demo_path.write_text(json.dumps(demo_event))
    demo.prepare(droot, demo_path, "https://demo.example.invalid")
    dbd = droot / demo.DATABASE_NAME
    with pytest.raises(prod.ProdError):
        prod.prepare(droot, config, ORIGIN, recipient)
    nested = droot / "empty"
    nested.mkdir(mode=0o700)
    with pytest.raises(prod.ProdError):
        prod.prepare(nested, config, ORIGIN, recipient)
    assert list(nested.iterdir()) == []
    with pytest.raises(prod.ProdError):
        prod.validate_database(dbd, prod_event, recipient)
    with pytest.raises(demo.DemoError):
        demo.validate_database(dbp, demo_event)
    with pytest.raises(prod.ProdError):
        prod.canonical(prod.DEMO_STATE / "collection.sqlite")
    demo.validate_database(dbd, demo_event)
    prod.validate_database(dbp, prod_event, recipient)


def test_serve_check_export_require_existing_storage_and_credentials(inputs):
    state, config, recipient = inputs
    for action in (prod.check, prod.prod_app):
        with pytest.raises((OSError, ValueError)):
            action(state, config, ORIGIN, recipient)
    assert not list(state.iterdir())
    dbp = prod.prepare(state, config, ORIGIN, recipient)
    demo_db = state.with_name("separate-demo.sqlite")
    prod.staff_admin.write_staff_password(demo_db, "demo-counter", secrets.token_urlsafe(24))
    prod.staff_admin.staff_key_path(dbp).write_bytes(prod.staff_admin.staff_key_path(demo_db).read_bytes())
    prod.staff_admin.staff_key_path(dbp).chmod(0o600)
    for action in (prod.check, prod.prod_app):
        with pytest.raises((OSError, ValueError)):
            action(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    out = state.with_name("refused-output")
    with pytest.raises((OSError, ValueError)):
        prod.export_qr(state, config, ORIGIN, recipient, out)
    assert not out.exists()


@pytest.mark.parametrize("fault", ["wrong-recipient", "profile", "event", "qr", "schema", "journal",
                                   "marker", "chmod", "hardlink", "symlink-directory"])
def test_storage_checks_detect_independent_failures(inputs, fault):
    state, config, recipient = inputs
    dbp = prod.prepare(state, config, ORIGIN, recipient)
    event = prod.load_prod_event(config)
    key = prod.application.default_profile_key_path(dbp)
    if fault == "wrong-recipient":
        other = state.with_name("other")
        other.mkdir(mode=0o700)
        _, descriptor = make_config(other)
        recipient = prod.contact.ProductionEnrollment.read(descriptor, event["id"])
    elif fault == "profile":
        key.write_bytes(secrets.token_bytes(32))
    elif fault == "event":
        event["reward"] += " revised"
    elif fault == "chmod":
        key.chmod(0o644)
    elif fault == "hardlink":
        os.link(dbp, state.parent / "alias.sqlite")
    elif fault == "symlink-directory":
        alias = state.with_name("alias")
        alias.symlink_to(state)
        dbp = alias / "instance" / prod.DATABASE_NAME
    else:
        with closing(sqlite3.connect(dbp)) as db:
            if fault == "qr":
                product = event["products"][0]["id"]
                db.execute("UPDATE products SET qr_key=? WHERE id=?", (secrets.token_urlsafe(24), product))
            elif fault == "schema":
                db.execute("DROP TRIGGER protect_correction_delete")
            elif fault == "journal":
                db.execute("PRAGMA journal_mode=DELETE")
            else:
                db.execute("DELETE FROM metadata WHERE key='linux_prod_v1'")
            db.commit()
    with pytest.raises((ValueError, OSError)):
        prod.validate_database(dbp, event, recipient)


def test_readonly_qr_export_eleven_verified_routes_and_locked_app(tmp_path, monkeypatch):
    state, config, _, recipient = ready_state(tmp_path)
    event = prod.load_prod_event(config)
    dbp = prod.database_path(state)
    with closing(prod.application.connect_db(dbp, mode="ro")) as db:
        before = list(db.iterdump())
        keys = dict(db.execute("SELECT id,qr_key FROM products"))
    def fail_if_initialized(*args, **kwargs):
        pytest.fail("Existing-state operation invoked application initialization")
    monkeypatch.setattr(prod.application, "init_db", fail_if_initialized)
    prod.check(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    client = prod.prod_app(state, config, ORIGIN, recipient, authority=authority_for(state, config)).test_client()
    with pytest.raises(prod.ProdError):
        prod.prod_app(state, config, "https://other.example.invalid", recipient)
    out = tmp_path.resolve() / "private-qr-output"
    prod.export_qr(state, config, ORIGIN, recipient, out)
    cards = json.loads((out / "manifest.json").read_text())
    assert len(cards) == 11
    assert cards[0] == {"kind": "entry", "file": "entry.svg", "url": ORIGIN + "/"}
    assert [c["product_id"] for c in cards[1:]] == [p["id"] for p in event["products"]]
    for card in cards:
        if card["kind"] == "product":
            assert card["url"] == ORIGIN + "/q/" + keys[card["product_id"]]
        assert ElementTree.parse(out / card["file"]).getroot().tag.endswith("svg")
        response = client.get(card["url"].removeprefix(ORIGIN), base_url=ORIGIN)
        assert response.status_code == 200
    assert all(p.stat().st_mode & 0o077 == 0 for p in out.iterdir())
    with pytest.raises(FileExistsError):
        prod.export_qr(state, config, ORIGIN, recipient, out)
    with pytest.raises(prod.ProdError):
        prod.export_qr(state, config, ORIGIN, recipient, state / "nested-qr")
    credential = prod.staff_admin.staff_key_path(dbp)
    os.link(credential, tmp_path / "other-file")
    with pytest.raises(prod.ProdError):
        prod.export_qr(state, config, ORIGIN, recipient, out.with_name("unpublished-qr"))
    with closing(prod.application.connect_db(dbp, mode="ro")) as db:
        assert list(db.iterdump()) == before


def test_sensitive_request_values_are_omitted_from_serve_log():
    sentinel = secrets.token_urlsafe(24)
    record = logging.LogRecord("waitress", logging.ERROR, __file__, 1, "/q/%s", (sentinel,), None)
    assert prod.PrivateFormatter().format(record) == "ERROR waitress"
