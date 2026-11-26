"""Release one PROD contact to a pinned SSH identity after committing its access audit."""
import argparse
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import select
import sys
import time

import prod


def require_open(access):
    if access.access() != "open":
        raise ValueError("Contact access unavailable")


def release(db, event, recipient, key_path, actor, request, *, access):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", actor) or not isinstance(request, dict)
            or set(request) != {"purpose", "customer_id"} or request["purpose"] not in ("pickup", "reward", "inquiry")
            or not isinstance(request["customer_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", request["customer_id"])):
        raise ValueError("Invalid contact request")
    require_open(access)
    cid = request["customer_id"]
    rid = secrets.token_hex(16)
    with db:
        db.execute("BEGIN IMMEDIATE")
        require_open(access)
        prod.validate_rows(db, event, key_path, recipient)
        row = db.execute("SELECT sealed_phone,key_id FROM customer_contacts WHERE customer_id=?", (cid,)).fetchone()
        if row and row[1] != recipient.key_id:
            raise ValueError("Invalid contact recipient")
        db.execute("INSERT INTO contact_access_audit VALUES (?,?,?,?,?,?)",
                   (rid, int(time.time()), actor, cid, request["purpose"], "released" if row else "unavailable"))
        require_open(access)
    require_open(access)
    if row is None:
        return {"status": "unavailable"}
    return {"status": "released", "request_id": rid, "key_id": row[1], "event_id": event["id"],
            "customer_id": cid, "sealed": base64.b64encode(row[0]).decode("ascii")}


def acknowledge(db, actor, rid, result, *, access):
    if result not in ("displayed", "decrypt_failed"):
        raise ValueError("Invalid acknowledgement")
    with db:
        db.execute("BEGIN IMMEDIATE")
        require_open(access)
        updated = db.execute("UPDATE contact_access_audit SET result=? WHERE request_id=? AND actor=? AND result='released'",
                             (result, rid, actor))
        if updated.rowcount != 1:
            raise ValueError("Unknown release")
        require_open(access)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--recipient", type=Path, required=True)
    parser.add_argument("--actor", required=True, help="Set by the forced command on the server")
    args = parser.parse_args()
    if os.environ.get("SSH_ORIGINAL_COMMAND") or not os.environ.get("SSH_CONNECTION") or sys.stdin.isatty():
        parser.exit(1, "Restricted SSH command required.\n")
    try:
        event = prod.load_prod_event(args.config)
        recipient = prod.contact.ProductionEnrollment.read(prod.canonical(args.recipient), event["id"])
        database = prod.database_path(args.state_dir)
        prod.check_files(database)
        key_path = prod.application.default_profile_key_path(database)
        access = prod.PrivacyAccess(args.state_dir, event, recipient)
        require_open(access)
        with closing(prod.application.connect_db(database, mode="rw")) as db:
            reply = release(db, event, recipient, key_path, args.actor, json.loads(sys.stdin.buffer.readline(512)), access=access)
            require_open(access)
            print(json.dumps(reply), flush=True)
            if reply["status"] == "released" and select.select([sys.stdin], [], [], 60)[0]:
                line = sys.stdin.buffer.readline(128)
                if line:
                    acknowledge(db, args.actor, reply["request_id"], json.loads(line)["result"], access=access)
    except Exception:
        parser.exit(1, "Contact lookup unavailable.\n")


if __name__ == "__main__":
    main()
