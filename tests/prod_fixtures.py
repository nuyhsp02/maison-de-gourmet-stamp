"""Disposable catalogue and public recipient for local PROD contract tests."""
import json
from pathlib import Path
import secrets
import sys

from nacl.public import PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "linux"))
import prod

ORIGIN = "https://store.example.invalid"


def make_config(directory, public_key=None, *, origin=ORIGIN):
    event = dict(environment="production", id="prod-synthetic-fixture", draft=False,
                 public_origin=origin, cafe_name="Synthetic café", title="Fixture event",
                 description="Synthetic event description", reward="Synthetic reward",
                 starts_at="2026-12-01T00:00:00+09:00", ends_at="2027-02-01T00:00:00+09:00",
                 reward_until="2027-03-01T00:00:00+09:00", access_until="2027-03-31T00:00:00+09:00",
                 products=[])
    for i in range(1, 11):
        pid = f"fixture-{i:02}"
        event["products"].append(dict(id=pid, category="season" if i <= 4 else "select",
                                      name=f"Fixture product {i}", short_name=f"Item {i}",
                                      description=f"Synthetic description {i}", qr_product_id=pid, image=None))
    config = directory / "fixture-event.json"
    config.write_text(json.dumps(event), encoding="utf-8")
    recipient = directory / "fixture-recipient.json"
    public_key = public_key if public_key is not None else PrivateKey.generate().public_key
    recipient.write_text(json.dumps(dict(format="prod-contact-recipient-v1", event_id=event["id"],
                                         public_key=bytes(public_key).hex())), encoding="utf-8")
    return config, recipient


def ready_state(directory, public_key=None):
    directory = directory.resolve()
    config, descriptor = make_config(directory, public_key)
    recipient = prod.contact.ProductionEnrollment.read(descriptor, "prod-synthetic-fixture")
    state = directory / "isolated-state"
    state.mkdir(mode=0o700)
    database = prod.prepare(state, config, ORIGIN, recipient)
    for role, actor in (("staff", "fixture-counter"), ("admin", "fixture-manager")):
        prod.staff_admin.write_staff_password(database, actor, secrets.token_urlsafe(24),
                                             role=role, production=True)
    return state, config, descriptor, recipient
