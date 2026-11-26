"""Provision disposable fixtures only. No private decryption key is written."""
from contextlib import closing
import json
from pathlib import Path
from nacl.public import PrivateKey


def write_config(contact, path, event_id, profiles, public_key=None):
    public_key = public_key or PrivateKey.generate().public_key
    values = sorted({contact.profile_tag(name, phone) for name, phone in profiles})
    with Path(path).open("x", encoding="utf-8") as dest:
        json.dump({"format": contact.NOTICE, "event_id": event_id, "public_key": bytes(public_key).hex(),
                   "allowed_profiles": values}, dest)
    return path


def prepare(module, config, database, path, profiles):
    event = module.load_event(config or module.ROOT / "event.json")
    if not path.exists():
        write_config(module.contact, path, event["id"], profiles)
    policy = module.contact.SyntheticEnrollment.read(path, event["id"])
    module.init_db(database, event)
    with closing(module.connect_db(database, mode="rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        module.contact.apply_contact_schema(db, policy.key_id)
    return path
