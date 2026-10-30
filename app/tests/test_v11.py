import json
import shutil
import subprocess
import time

import pytest

from app import ROOT, connect_db, create_app, digest
from conftest import BASE, Visitor


NAVIGATION = {"Sec-Fetch-User": "?1", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


@pytest.mark.parametrize("headers,eligible", [
    (NAVIGATION, True), ({}, False),
    ({**NAVIGATION, "Purpose": "prefetch"}, False),
    ({**NAVIGATION, "Sec-Purpose": "prefetch;prerender"}, False),
    ({**NAVIGATION, "X-Purpose": "preview"}, False),
    ({**NAVIGATION, "Sec-Fetch-User": ""}, False),
    ({**NAVIGATION, "Sec-Fetch-Mode": "cors"}, False),
    ({**NAVIGATION, "Sec-Fetch-Dest": "iframe"}, False),
])
def test_navigation_hint_is_read_only_and_prefetch_fails_closed(app, keys, headers, eligible):
    v = Visitor(app)
    before = v.read()["customer"]
    response = v.client.get("/q/" + keys[0], base_url=BASE, headers=headers)
    assert response.status_code == 200
    assert ('data-auto-collect="eligible"' in response.text) == eligible
    assert v.read()["customer"] == before
    assert "Set-Cookie" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    with connect_db(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM stamps").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1


def test_new_visitor_get_stays_anonymous_even_if_navigation_eligible(app, keys):
    v = Visitor(app, start=False)
    assert v.client.get("/q/" + keys[0], base_url=BASE, headers=NAVIGATION).status_code == 200
    assert v.read()["customer"] is None
    with connect_db(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_v11_single_button_markup_and_core_are_present(app):
    response = app.test_client().get("/", base_url=BASE)
    assert response.text.count('id="redeem-button"') == 1
    assert 'id="redeem-dialog"' not in response.text
    assert 'id="received-checkbox"' not in response.text
    assert 'id="confirm-redeem"' not in response.text
    assert 'id="viewfinder"' in response.text
    assert '직원 인증이 없는 버튼' in response.text
    assert response.text.index('id="reward-warning"') < response.text.index('id="redeem-button"')
    assert '/static/collection-core.js' in response.text


def test_v1_data_cookie_recovery_link_and_fixed_qr_survive_restart(tmp_path):
    # V1's unchanged tables with pre-existing records. No reset/migration or token
    # reissuance may be required to start V1.1 against this database.
    database = tmp_path / "v1.sqlite"
    event = json.loads((ROOT / "event.json").read_text())
    ids = sorted(p["id"] for p in event["products"])
    now = int(time.time())
    cookie, recovery, customer_id = "s" * 43, "r" * 43, "legacy-customer"
    with connect_db(database) as db:
        db.executescript((ROOT / "tests" / "fixtures" / "v1-schema.sql").read_text())
        db.execute("INSERT INTO metadata VALUES ('event', ?)", (json.dumps([event["id"], ids]),))
        db.execute("INSERT INTO customers VALUES (?, ?, NULL)", (customer_id, now - 100))
        for i, product_id in enumerate(ids):
            db.execute("INSERT INTO products VALUES (?, ?)", (product_id, str(i).zfill(32)))
        for product_id in ids[:4]:
            db.execute("INSERT INTO stamps VALUES (?, ?, ?)", (customer_id, product_id, now - 50))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (digest(cookie), customer_id, "legacy-csrf", now + 3600))
        db.execute("INSERT INTO recovery VALUES (?, ?, ?)", (customer_id, digest(recovery), now + 3600))
        before = list(db.iterdump())
    app = create_app(database=database, public_url=BASE, dev=True, testing=True)
    with connect_db(database) as db:
        assert list(db.iterdump()) == before
    v = Visitor(app, cookie=cookie)
    assert v.read()["customer"]["count"] == 4
    assert v.csrf == "legacy-csrf"
    assert v.client.get("/q/" + "0".zfill(32), base_url=BASE).status_code == 200
    fresh = Visitor(app, start=False)
    assert fresh.post("/api/recover", {"token": recovery}).status_code == 200
    assert fresh.read()["customer"]["count"] == 4
    assert fresh.collect("4".zfill(32))["added"] is True
    assert v.read()["customer"]["count"] == 5
    with connect_db(database) as db:
        assert db.execute("SELECT token_hash FROM recovery").fetchone()[0] == digest(recovery)
        assert [r[0] for r in db.execute("SELECT qr_key FROM products ORDER BY id")] == [str(i).zfill(32) for i in range(10)]


def test_frontend_scanner_and_customer_flow_regressions():
    node = shutil.which("node")
    assert node, "V1.1 프런트엔드 테스트에는 Node.js 18 이상이 필요합니다."
    result = subprocess.run([node, "--test", "tests/scanner.test.cjs", "tests/app-flow.test.cjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
