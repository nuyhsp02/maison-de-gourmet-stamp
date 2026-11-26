"""Private photos and after-hours catalogue maintenance, using synthetic files and DB only."""
import base64
from contextlib import closing
import hashlib
import json
import os
import sqlite3

import pytest

from lifecycle_fixtures import authority_for, before_privacy_cutoff
from prod_fixtures import ORIGIN, make_config, prod, ready_state


pytestmark = [pytest.mark.usefixtures("before_privacy_cutoff"),
              pytest.mark.skipif(os.name != "posix", reason="Production runs on POSIX")]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII=")


def add_photos(config):
    doc = json.loads(config.read_text())
    digest = hashlib.sha256(PNG).hexdigest()
    files = config.parent / "product-photos" / digest
    files.mkdir(parents=True, exist_ok=True)
    for product in doc["products"]:
        name = product["id"] + ".png"
        product["image"] = f"/product-photos/{digest}/{name}"
        (files / name).write_bytes(PNG)
    doc["products"][0]["description"] += " Revised."
    candidate = config.with_name("catalogue-next.json")
    candidate.write_text(json.dumps(doc), encoding="utf-8")
    return candidate


def db_snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


def test_prod_prepare_accepts_explicitly_empty_descriptions(tmp_path):
    root = tmp_path.resolve()
    config, descriptor = make_config(root)
    candidate = add_photos(config)
    event = json.loads(candidate.read_text())
    for item in event["products"]:
        item["description"] = ""
    candidate.write_text(json.dumps(event))
    recipient = prod.contact.ProductionEnrollment.read(descriptor, event["id"])
    state = root / "state"
    state.mkdir(mode=0o700)
    database = prod.prepare(state, candidate, ORIGIN, recipient)
    prod.validate_database(database, prod.load_prod_event(candidate), recipient)


@pytest.mark.parametrize("invalid", ["  ", "\t\n", 0, False, []])
def test_prod_description_refuses_blank_whitespace_and_nontext(tmp_path, invalid):
    config, _ = make_config(tmp_path.resolve())
    event = json.loads(config.read_text())
    event["products"][0]["description"] = invalid
    config.write_text(json.dumps(event))
    with pytest.raises(prod.ProdError):
        prod.load_prod_event(config)


def test_photo_routes_require_the_listed_path_and_authentic_bytes(tmp_path):
    state, config, _, recipient = ready_state(tmp_path)
    next_config = add_photos(config)
    auth = authority_for(state, config)
    prod.update_catalog(state, config, next_config, ORIGIN, recipient, authority=auth)
    event = prod.load_prod_event(next_config)
    prod.check(state, next_config, ORIGIN, recipient, authority=auth)
    client = prod.prod_app(state, next_config, ORIGIN, recipient, authority=auth).test_client()
    for p in event["products"]:
        response = client.get(p["image"], base_url=ORIGIN)
        assert response.status_code == 200
        assert response.mimetype == "image/png" and response.data == PNG
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    url = event["products"][0]["image"]
    path = config.parent / url.lstrip("/")
    (path.parent / "unlisted.png").write_bytes(PNG)
    for bad in (url.replace(path.name, "unlisted.png"), url.replace(".png", ".svg"), "/product-photos/event.json"):
        assert client.get(bad, base_url=ORIGIN).status_code == 404
    path.unlink()
    with pytest.raises(FileNotFoundError):
        prod.check(state, next_config, ORIGIN, recipient, authority=auth)
    assert client.get(url, base_url=ORIGIN).status_code == 404
    path.write_bytes(PNG + b"changed")
    assert client.get(url, base_url=ORIGIN).status_code == 404
    path.unlink()
    path.symlink_to(path.with_name("unlisted.png"))
    assert client.get(url, base_url=ORIGIN).status_code == 404
    path.unlink()
    os.link(path.with_name("unlisted.png"), path)
    assert client.get(url, base_url=ORIGIN).status_code == 404
    path.unlink()
    path.write_bytes(PNG)
    digest_dir = path.parent
    moved = digest_dir.with_name("real")
    digest_dir.rename(moved)
    digest_dir.symlink_to(moved, target_is_directory=True)
    assert client.get(url, base_url=ORIGIN).status_code == 404
    auth.closed = True
    assert client.get(url, base_url=ORIGIN).status_code == 410


@pytest.mark.parametrize("target", [
    "https://outer.example.invalid/photo.png", "//outer.example.invalid/p.png", "/etc/passwd",
    "/product-photos/" + "a"*64 + "/../fixture-01.png",
    "/product-photos/" + "a"*64 + "/fixture-02.png",
    "/product-photos/" + "a"*64 + "/fixture-01.svg",
    "/product-photos/short/fixture-01.png",
])
def test_reject_unsafe_or_mismatched_photo_reference(tmp_path, target):
    config, _ = make_config(tmp_path)
    event = json.loads(config.read_text())
    event["products"][0]["image"] = target
    config.write_text(json.dumps(event))
    with pytest.raises(prod.ProdError):
        prod.load_prod_event(config)


def test_bind_only_new_catalogue_hmac_and_preserve_records(tmp_path):
    state, config, _, recipient = ready_state(tmp_path)
    database = prod.database_path(state)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO customers VALUES ('fixture',1,3)")
        db.executemany("INSERT INTO stamps VALUES ('fixture',?,2)", db.execute("SELECT id FROM products").fetchall())
        db.execute("INSERT INTO reward_audit VALUES ('fixture',3,'unauthenticated_customer_screen')")
        db.execute("INSERT INTO sessions VALUES ('synthetic','fixture','csrf',100)")
    keys = {p: hashlib.sha256(p.read_bytes()).digest() for p in database.parent.glob("*.key")}
    before = db_snapshot(database)
    candidate = add_photos(config)
    old, new = map(prod.load_prod_event, (config, candidate))
    auth = authority_for(state, config)

    # Swapping the JSON alone must not open the original DB under a changed description or image.
    with pytest.raises(prod.ProdError):
        prod.validate_database(database, new, recipient)
    with prod.catalogue_lock(database, serving=True), pytest.raises(BlockingIOError):
        prod.update_catalog(state, config, candidate, ORIGIN, recipient, authority=auth)
    assert db_snapshot(database) == before

    prod.update_catalog(state, config, candidate, ORIGIN, recipient, authority=auth)
    after = db_snapshot(database)
    diff = set(after).symmetric_difference(before)
    assert len(diff) == 2 and all("INSERT INTO \"metadata\" VALUES('prod_inventory_v1'," in s for s in diff)
    assert keys == {p: hashlib.sha256(p.read_bytes()).digest() for p in keys}
    prod.validate_database(database, new, recipient)
    with pytest.raises(prod.ProdError):
        prod.validate_database(database, old, recipient)
    # Lost the command's response before the operator installed the prepared config.
    prod.update_catalog(state, config, candidate, ORIGIN, recipient, authority=auth)
    assert db_snapshot(database) == after
    # A deliberate reversal uses both verified revisions, without a DB restore.
    prod.update_catalog(state, candidate, config, ORIGIN, recipient, authority=auth)
    assert db_snapshot(database) == before
    prod.validate_database(database, old, recipient)


@pytest.mark.parametrize("fault", ["reward", "order", "name", "tampered-qr", "missing-file", "wrong-format", "closed", "rollback"])
def test_failed_catalogue_changes_leave_database_unchanged(tmp_path, fault, monkeypatch):
    state, config, _, recipient = ready_state(tmp_path)
    database = prod.database_path(state)
    auth = authority_for(state, config)
    candidate = add_photos(config)
    event = json.loads(candidate.read_text())
    p = event["products"][0]
    if fault == "reward":
        event["reward"] += "!"
    elif fault == "name":
        p["name"] += "!"
    elif fault == "order":
        event["products"].reverse()
    elif fault == "tampered-qr":
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE products SET qr_key=? WHERE id=?", ("x"*32, p["id"]))
    elif fault == "missing-file":
        (config.parent / p["image"].lstrip("/")).unlink()
    elif fault == "wrong-format":
        data = b"not a raster image"
        digest = hashlib.sha256(data).hexdigest()
        p["image"] = f"/product-photos/{digest}/{p['id']}.png"
        file = config.parent / p["image"].lstrip("/")
        file.parent.mkdir()
        file.write_bytes(data)
    elif fault == "closed":
        auth.closed = True
    else:
        original = prod.validate_rows
        def fail_final(db, target, *args, **opts):
            if target == event:
                assert original(db, target, *args, **opts)
                raise prod.ProdError("Simulated post-update verification failure")
            return original(db, target, *args, **opts)
        monkeypatch.setattr(prod, "validate_rows", fail_final)
    candidate.write_text(json.dumps(event))
    before = db_snapshot(database)
    with pytest.raises((ValueError, OSError)):
        prod.update_catalog(state, config, candidate, ORIGIN, recipient, authority=auth)
    assert db_snapshot(database) == before
