"""One forced-command SSH session. Only ciphertext is written to its pipe."""
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

from demo import (DATABASE_NAME, check_private, connect_db, default_profile_key_path,
                  load_demo_event, validate_rows)

PURPOSES = {"pickup", "reward", "inquiry"}


def release(database, event, actor, request):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", actor) or not isinstance(request, dict)
            or set(request) != {"customer_id", "purpose"} or request["purpose"] not in PURPOSES
            or not re.fullmatch(r"[a-f0-9]{32}", request["customer_id"])):
        raise ValueError("Invalid request")
    cid = request["customer_id"]
    rid = secrets.token_hex(16)
    check_private(database.parent, directory=True)
    check_private(database)
    key_path = default_profile_key_path(database)
    check_private(key_path)
    with closing(connect_db(database, mode="rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        validate_rows(db, event, key_path, require_wal=True)
        row = db.execute("SELECT sealed_phone,key_id FROM customer_contacts WHERE customer_id=?", (cid,)).fetchone()
        db.execute("INSERT INTO contact_access_audit VALUES (?,?,?,?,?,?)",
                   (rid, int(time.time()), actor, cid, request["purpose"], "released" if row else "unavailable"))
    if row is None:
        return {"status": "unavailable"}
    return {"status": "released", "request_id": rid, "key_id": row["key_id"],
            "event_id": event["id"], "customer_id": cid, "sealed": base64.b64encode(row["sealed_phone"]).decode("ascii")}


def acknowledge(database, actor, rid, status):
    if status not in ("displayed", "decrypt_failed"):
        raise ValueError("Invalid acknowledgement")
    with closing(connect_db(database, mode="rw")) as db, db:
        updated = db.execute("UPDATE contact_access_audit SET result=? "
                             "WHERE request_id=? AND actor=? AND result='released'", (status, rid, actor))
        if updated.rowcount != 1:
            raise ValueError("Unknown release")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--actor", required=True, help="Fixed by the server's authorized_keys entry")
    args = parser.parse_args()
    if os.environ.get("SSH_ORIGINAL_COMMAND") or not os.environ.get("SSH_CONNECTION") or sys.stdin.isatty():
        parser.exit(1, "Restricted SSH command required.\n")
    database = args.state_dir / DATABASE_NAME
    try:
        request = json.loads(sys.stdin.buffer.readline(512))
        reply = release(database, load_demo_event(args.config), args.actor, request)
        print(json.dumps(reply), flush=True)
        if reply["status"] == "released" and select.select([sys.stdin], [], [], 60)[0]:
            line = sys.stdin.buffer.readline(128)
            if line:
                acknowledge(database, args.actor, reply["request_id"], json.loads(line)["result"])
    except Exception:
        parser.exit(1, "Contact lookup unavailable.\n")


if __name__ == "__main__":
    main()
