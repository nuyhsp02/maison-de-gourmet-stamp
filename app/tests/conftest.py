import sys
from itertools import count
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ROOT, connect_db, create_app

BASE = "http://127.0.0.1:8765"
_visitor_ids = count(1)


sys.path.insert(0, str(ROOT.parent / "tools"))
import contact_fixture
import app as application


def fixture_app(event_path=None, database=None, public_url=BASE, **kwargs):
    config = Path(database).parent / "synthetic-contact.json"
    profiles = [("테스트 방문자", f"010{i:08d}") for i in range(1, 3001)]
    profiles.extend([("Profile Test Visitor", "01000000081"), ("Different Test Visitor", "01000000081"),
                     ("별도 합성", "01000000082"), ("Synthetic Admin Test Visitor", "01000000074")])
    contact_fixture.prepare(application, event_path, database, config, profiles)
    return create_app(event_path, database, public_url, synthetic_config=config, **kwargs)


def registration():
    return {"name": "테스트 방문자", "phone": f"010{next(_visitor_ids):08d}", "notice_acknowledged": True, "notice_version": application.NOTICE_VERSION}


@pytest.fixture
def app(tmp_path):
    return fixture_app(database=tmp_path / "test.sqlite", public_url=BASE, dev=True, testing=True)


@pytest.fixture
def keys(app):
    with connect_db(app.config["DATABASE"]) as db:
        return [row[0] for row in db.execute("SELECT qr_key FROM products ORDER BY id")]


class Visitor:
    def __init__(self, app, start=True, cookie=None, base=BASE):
        self.app, self.base = app, base
        self.domain = urlsplit(base).hostname
        self.client = app.test_client()
        if cookie:
            self.client.set_cookie(app.cookie_name, cookie, domain=self.domain)
        self.csrf = None
        if start and not cookie:
            assert self.post("/api/register", registration()).status_code == 200
        self.read()

    def read(self):
        response = self.client.get("/api/state", base_url=self.base)
        assert response.status_code == 200
        self.state = response.json
        self.csrf = self.state["csrf"]
        return self.state

    def post(self, path, body):
        return self.client.post(path, json=body, base_url=self.base, headers={
            "Origin": self.base, "X-Stamp-Client": "1", "X-CSRF-Token": self.csrf or ""})

    def collect(self, key):
        response = self.post("/api/collect", {"qr": key})
        assert response.status_code == 200, response.json
        return response.json

    def cookie(self):
        return self.client.get_cookie(self.app.cookie_name, domain=self.domain).value
