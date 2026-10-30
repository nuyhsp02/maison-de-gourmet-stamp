import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ROOT, connect_db, create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def app(tmp_path):
    return create_app(database=tmp_path / "test.sqlite", public_url=BASE, dev=True, testing=True)


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
            assert self.post("/api/session", {}).status_code == 200
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


_LOAD_DIAGNOSTICS = []


@pytest.fixture(autouse=True)
def observe_load_storage(request):
    """Keep the original 400/30 workload unchanged; observe only this test."""
    if request.node.name == 'test_native_400_visitors_30_concurrent':
        from concurrency_support import failure_counts, runtime_info
        caplog = request.getfixturevalue('caplog')
        yield
        # No lock wrapper, teardown probe, sleeps or retries on this path.
        _LOAD_DIAGNOSTICS.append({'mode': 'native_no_observer', 'runtime': runtime_info(),
                                  **failure_counts(caplog.get_records('call'))})
        return
    if request.node.name != 'test_400_visitors_30_concurrent':
        yield
        return
    from flask import g
    from concurrency_support import ObservedLock
    app = request.getfixturevalue('app')
    caplog = request.getfixturevalue('caplog')
    observed = ObservedLock(app.write_lock)
    app.write_lock = observed

    @app.teardown_appcontext
    def observe_before_close(_error):
        connection = g.get('db')
        if connection is not None:
            observed.observe_transaction(connection.in_transaction)

    yield
    # Only static runtime labels, counts and durations; no request/DB values.
    summary = observed.summary(caplog.get_records('call'))
    _LOAD_DIAGNOSTICS.append(summary)
    request.node.user_properties.append(('storage_diagnostics', json.dumps(summary)))


def pytest_terminal_summary(terminalreporter):
    for summary in _LOAD_DIAGNOSTICS:
        terminalreporter.write_line('STORAGE_DIAGNOSTICS ' + json.dumps(summary))
