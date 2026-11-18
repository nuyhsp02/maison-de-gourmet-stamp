"""Upgrade a synthetic PR 1 card without touching its original backup."""
import hmac
import json
import secrets
import time
from contextlib import closing
from pathlib import Path

from app import ROOT, connect_db, create_app, digest
from conftest import BASE, Visitor


def test_pr1_backup_keeps_profiles_sessions_rewards_and_qr_through_two_starts(tmp_path):
    source = tmp_path / "pr1.sqlite"
    working = tmp_path / "upgrade.sqlite"
    backup_key = secrets.token_bytes(32)
    original_key = Path(str(source) + ".profile.key")
    original_key.write_bytes(backup_key)
    cookie, link, csrf = [secrets.token_urlsafe(32) for _ in range(3)]
    event = json.loads((ROOT / "event.json").read_text())
    ids = sorted(p["id"] for p in event["products"])
    expiry = int(time.time()) + 3600
    profile = {"name": "시연 고객", "phone": "01000000005"}
    with closing(connect_db(source)) as db:
        db.executescript((ROOT / "tests/fixtures/pr1-schema.sql").read_text())
        db.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("event", json.dumps([event["id"], ids])),
            ("profile_key_check_v1", hmac.digest(backup_key, b"maison/profile-key-check/v1", "sha256").hex()),
        ])
        db.executemany("INSERT INTO products VALUES (?, ?)", [(p, secrets.token_urlsafe(24)) for p in ids])
        db.execute("INSERT INTO customers VALUES ('existing', 5, 30)")
        lookup = hmac.digest(backup_key, profile["phone"].encode("ascii"), "sha256").hex()
        db.execute("INSERT INTO customer_profiles VALUES ('existing', ?, ?, 'demo-v1', 5)",
                   (profile["name"], lookup))
        db.execute("INSERT INTO sessions VALUES (?, 'existing', ?, ?)", (digest(cookie), csrf, expiry))
        db.execute("INSERT INTO recovery VALUES ('existing', ?, ?)", (digest(link), expiry))
        db.executemany("INSERT INTO stamps VALUES ('existing', ?, 20)", [(p,) for p in ids])
        db.execute("INSERT INTO reward_audit VALUES ('existing', 30, 'unauthenticated_customer_screen')")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        original_rows = {t: set(map(tuple, db.execute(f"SELECT * FROM {t}"))) for t in tables}
        with closing(connect_db(working)) as clone:
            db.backup(clone)
    source_bytes = source.read_bytes()
    key_path = Path(str(working) + ".profile.key")
    key_path.write_bytes(backup_key)
    for _ in range(2):
        app = create_app(database=working, public_url=BASE, dev=True, testing=True)
        with closing(connect_db(working)) as db:
            for table, rows in original_rows.items():
                migrated = set(map(tuple, db.execute(f"SELECT * FROM {table}")))
                if table == "metadata":
                    assert rows <= migrated  # PR 2 adds its own schema marker.
                else:
                    assert rows == migrated
            assert db.execute("SELECT COUNT(*) FROM stamp_cancel_audit").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    card = Visitor(app, cookie=cookie).state["customer"]
    assert card["count"] == 10 and card["status"] == "redeemed"
    found = Visitor(app, start=False)
    assert found.post("/api/find", profile).status_code == 200
    assert found.read()["customer"] == card
    recovered = Visitor(app, start=False)
    assert recovered.post("/api/recover", {"token": link}).status_code == 200
    assert recovered.read()["customer"] == card
    assert key_path.read_bytes() == original_key.read_bytes() == backup_key
    assert source.read_bytes() == source_bytes
