"""Loopback-only, disposable custody authority for integration tests."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/linux"))
import prod_authority as custody


def approved_test_policy(days=3):
    # Arbitrary synthetic approvals, never operator decisions.
    return {"retention_days": days, **{k: hashlib.sha256(k.encode()).hexdigest() for k in custody.APPROVALS}}


def certificates(directory):
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)

    for peer, purpose in (("server", "serverAuth"), ("client", "clientAuth")):
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Fixture CA",
            "-keyout",
            peer + "-ca.key",
            "-out",
            peer + "-ca.pem",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        )
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-keyout",
            peer + ".key",
            "-out",
            peer + ".csr",
        )
        ext = directory / (peer + ".ext")
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
            + "extendedKeyUsage="
            + purpose
            + "\n"
        )
        openssl(
            "x509",
            "-req",
            "-in",
            peer + ".csr",
            "-CA",
            peer + "-ca.pem",
            "-CAkey",
            peer + "-ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            ext.name,
            "-out",
            peer + ".pem",
        )
        (directory / (peer + ".key")).chmod(0o600)


@contextmanager
def tls_authority(tmp, event_id, instance_id, cutoff, *, clock=None, allow_receipt=True):
    with tempfile.TemporaryDirectory(dir=Path(tmp).resolve()) as folder:
        directory = Path(folder)
        ledger = custody.CustodyLedger.create(
            directory / "custody.sqlite",
            event_id,
            instance_id,
            cutoff,
            approved_test_policy(),
            clock=clock or (lambda: cutoff - 1),
        )
        certificates(directory)
        peer = hashlib.sha256(ssl.PEM_cert_to_DER_cert((directory / "client.pem").read_text())).hexdigest()
        with custody.make_server(
            ledger,
            ("127.0.0.1", 0),
            cert=directory / "server.pem",
            key=directory / "server.key",
            client_ca=directory / "client-ca.pem",
            live_peer_sha256=peer if allow_receipt else None,
        ) as server:
            runner = threading.Thread(target=server.serve_forever, daemon=True)
            runner.start()
            descriptor = directory / "connection.json"
            descriptor.write_text(
                json.dumps(
                    {
                        "url": f"https://127.0.0.1:{server.server_port}",
                        "ca": str(directory / "server-ca.pem"),
                        "cert": str(directory / "client.pem"),
                        "key": str(directory / "client.key"),
                    }
                )
            )
            try:
                yield descriptor
            finally:
                server.shutdown()
                runner.join(5)
