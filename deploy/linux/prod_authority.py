"""Historical ADR-012 transport for replaying the original custody protocol tests."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
from urllib.parse import urlsplit

from prod_custody import (
    APPROVALS, CUTOFF, INVENTORY_KINDS, TIMEOUT, AuthorityError, CustodyLedger, Status,
    digest, trusted_file, validate_binding,
)

MAX_BODY = 8192


ACTIONS = {
    "status": (),
    "close": (),
    "record_purge": (),
    "reserve": ("kind", "object_id"),
    "check_archive": ("object_id",),
}


def make_server(ledger, address, *, cert, key, client_ca, live_peer_sha256=None):
    """Listen only on the custodian host; use dedicated CA for the permitted peer identities."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (
                    self.path != "/v1/lifecycle"
                    or not (0 < size <= MAX_BODY)
                    or self.headers.get("Transfer-Encoding")
                ):
                    raise AuthorityError("Invalid request")
                data = json.loads(self.rfile.read(size))
                action = data["action"]
                if (
                    not digest(data["nonce"])
                    or action not in ACTIONS
                    or set(data) != {"nonce", "action", "event_id", "instance_id", "cutoff", *ACTIONS[action]}
                ):
                    raise AuthorityError("Invalid request")
                binding = (data["event_id"], data["instance_id"], data["cutoff"])
                if action == "record_purge" or (action == "close" and not ledger.status(*binding).closed):
                    peer_digest = hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
                    if not digest(live_peer_sha256) or not hmac.compare_digest(peer_digest, live_peer_sha256):
                        raise AuthorityError("Active instance certificate required")
                arguments = [data[k] for k in ("event_id", "instance_id", "cutoff", *ACTIONS[action])]
                status = getattr(ledger, action)(*arguments)
                result = {
                    "nonce": data["nonce"],
                    "event_id": data["event_id"],
                    "cutoff": data["cutoff"],
                    "status": asdict(status),
                }
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (KeyError, ValueError, TypeError, sqlite3.Error, OSError):
                self.send_error(403, "Custody check refused")

        def log_message(self, *_):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(trusted_file(client_ca)))
    context.load_cert_chain(trusted_file(cert), trusted_file(key, private=True))

    class Server(ThreadingHTTPServer):
        # Do not block the accept loop on an unauthenticated TLS handshake.
        def get_request(self):
            stream, peer = super().get_request()
            stream.settimeout(TIMEOUT)
            try:
                return context.wrap_socket(stream, server_side=True, do_handshake_on_connect=False), peer
            except BaseException:
                stream.close()
                raise

        def handle_error(self, request, client_address):
            pass

    return Server(address, Handler)


class Authority:
    def __init__(self, descriptor):
        try:
            cfg = json.loads(trusted_file(descriptor).read_bytes())
            if set(cfg) != {"url", "ca", "cert", "key"}:
                raise AuthorityError("Malformed authority descriptor")
            url = urlsplit(cfg["url"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise AuthorityError("Dedicated HTTPS authority origin required")
            self.host, self.port = url.hostname, url.port or 443
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.context.load_verify_locations(cafile=str(trusted_file(cfg["ca"])))
            self.context.load_cert_chain(trusted_file(cfg["cert"]), trusted_file(cfg["key"], private=True))
        except (ValueError, OSError, TypeError, KeyError):
            raise AuthorityError("Independent lifecycle authority configuration unavailable") from None

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("MAISON_PROD_AUTHORITY", ""))

    def _request(self, action, event_id, instance_id, cutoff, **extra):
        validate_binding(event_id, instance_id, cutoff)
        nonce = secrets.token_hex(32)
        data = dict(
            action=action, event_id=event_id, instance_id=instance_id, cutoff=cutoff, nonce=nonce, **extra
        )
        connection = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=TIMEOUT)
        try:
            connection.request("POST", "/v1/lifecycle", json.dumps(data), {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            if response.status != 200 or len(raw) > MAX_BODY:
                raise AuthorityError("Custody request refused")
            reply = json.loads(raw)
            s = Status(**reply["status"])
            if (
                not hmac.compare_digest(nonce, reply["nonce"])
                or reply["event_id"] != event_id
                or reply["cutoff"] != cutoff
                or not digest(s.instance_id)
                or instance_id not in (None, s.instance_id)
                or any(
                    type(getattr(s, k)) is not bool
                    for k in ("ready", "backup_ready", "purged", "backups_expired", "closed", "purge_authorized")
                )
                or (s.retention_days is not None and (type(s.retention_days) is not int or s.retention_days <= 0))
                or (s.ready and not s.backup_ready)
                or (s.backup_ready and s.retention_days is None)
                or ((s.purged or s.backups_expired or s.purge_authorized) and not s.closed)
            ):
                raise AuthorityError("Custody proof mismatch")
            return s
        except (ValueError, OSError, TypeError, KeyError, http.client.HTTPException):
            raise AuthorityError("Live custody proof unavailable") from None
        finally:
            connection.close()

    def status(self, event_id, instance_id, cutoff):
        return self._request("status", event_id, instance_id, cutoff)

    def close(self, event_id, instance_id, cutoff):
        return self._request("close", event_id, instance_id, cutoff)

    def reserve(self, event_id, instance_id, cutoff, kind, object_id):
        return self._request("reserve", event_id, instance_id, cutoff, kind=kind, object_id=object_id)

    def check_archive(self, event_id, instance_id, cutoff, object_id):
        return self._request("check_archive", event_id, instance_id, cutoff, object_id=object_id)

    def record_purge(self, event_id, instance_id, cutoff):
        return self._request("record_purge", event_id, instance_id, cutoff)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument(
        "--live-peer-sha256", required=True, help="Independently pinned active app client certificate DER SHA-256"
    )
    for key in ("cert", "key", "client-ca"):
        serve.add_argument("--" + key, type=Path, required=True)
    for name in ("create", "approve", "status", "objects", "discover", "flag-unknown", "attest", "reconcile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--event", required=True)
        cmd.add_argument("--instance", required=True)
        if name in ("create", "approve"):
            cmd.add_argument("--policy", type=Path, required=True)
        if name in ("discover", "reconcile"):
            cmd.add_argument("--kind", choices=INVENTORY_KINDS, required=True)
        if name in ("discover", "flag-unknown", "attest"):
            cmd.add_argument("--object-id", required=True)
        if name in ("attest", "reconcile"):
            cmd.add_argument("--receipt-sha256", required=True)
            cmd.add_argument("--receipt", type=Path, required=True, help="Custodian-verified provider evidence")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "serve":
            with make_server(
                CustodyLedger(args.ledger),
                (args.listen, args.port),
                cert=args.cert,
                key=args.key,
                client_ca=args.client_ca,
                live_peer_sha256=args.live_peer_sha256,
            ) as server:
                server.serve_forever()
            return
        binding = (args.event, args.instance, CUTOFF)
        if args.action in {"attest", "reconcile"}:
            with trusted_file(args.receipt, private=True).open("rb") as proof:
                receipt_hash = hashlib.sha256()
                for chunk in iter(lambda: proof.read(65536), b""):
                    receipt_hash.update(chunk)
                receipt = receipt_hash.hexdigest()
            if not hmac.compare_digest(receipt, args.receipt_sha256):
                raise AuthorityError("External evidence differs from the independent receipt")
        if args.action in ("create", "approve"):
            policy = json.loads(trusted_file(args.policy).read_bytes())
            if args.action == "create":
                ledger = CustodyLedger.create(args.ledger, *binding, policy)
                result = ledger.status(*binding)
            else:
                result = CustodyLedger(args.ledger).approve(*binding, policy)
        else:
            ledger = CustodyLedger(args.ledger)
            if args.action == "discover":
                result = ledger.discover(*binding, args.kind, args.object_id)
            elif args.action == "flag-unknown":
                result = ledger.flag_unknown(*binding, args.object_id)
            elif args.action == "attest":
                result = ledger.attest_deleted(*binding, args.object_id, args.receipt_sha256)
            elif args.action == "reconcile":
                result = ledger.reconcile(*binding, args.kind, args.receipt_sha256)
            else:
                result = getattr(ledger, args.action)(*binding)
        print(json.dumps(result if isinstance(result, list) else asdict(result), sort_keys=True))
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
        parser.exit(1, "Custody operation refused. Verify protected inputs and independent records.\n")


if __name__ == "__main__":
    main()
