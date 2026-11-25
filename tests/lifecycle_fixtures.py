"""Mutable independent clock/status used only by isolated PROD route tests."""
from contextlib import closing
from types import SimpleNamespace

import pytest

from prod_fixtures import prod


class MutableAuthority:
    def __init__(self, event_id, instance_id, cutoff, clock=None):
        self.binding = (event_id, instance_id, cutoff)
        self.clock = clock or (lambda: cutoff - 10)
        self.available = True
        self.closed = False
        self.ready = True
        self.purge_authorized = True
        self.purged = False
        self.backups_expired = False

    def status(self, event_id, instance_id, cutoff):
        if not self.available or (event_id, instance_id, cutoff) != self.binding:
            raise ValueError("Synthetic custody state unavailable")
        if self.clock() >= cutoff:
            self.closed = True
        return SimpleNamespace(instance_id=self.binding[1], closed=self.closed, ready=self.ready,
                               purge_authorized=self.purge_authorized, purged=self.purged,
                               backups_expired=self.backups_expired)

    def close(self, *binding):
        self.status(*binding)
        self.closed = True
        return self.status(*binding)

    def record_purge(self, *binding):
        status = self.status(*binding)
        if not status.closed or not status.purge_authorized:
            raise ValueError("No synthetic approval")
        self.purged = True
        return self.status(*binding)


def authority_for(state, config):
    event = prod.load_prod_event(config)
    with closing(prod.application.connect_db(prod.database_path(state), mode="ro")) as db:
        key = db.execute("SELECT value FROM metadata WHERE key='prod_instance_id'").fetchone()[0]
    return MutableAuthority(event["id"], key, prod.application.timestamp(event["access_until"]))


# Freeze only the entry point clock. App routes may vary their own time in existing tests.
@pytest.fixture
def before_privacy_cutoff(monkeypatch):
    monkeypatch.setattr(prod, "time", SimpleNamespace(time=lambda: fixture_epoch()))


def fixture_epoch():
    return prod.application.timestamp("2026-12-12T12:00:00+09:00")


if __name__ == "__main__":
    # Launch the real CLI with a private synthetic clock in the HTTPS subprocess.
    prod.time = SimpleNamespace(time=fixture_epoch)
    prod.main()
