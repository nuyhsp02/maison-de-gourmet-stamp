"""Run the shipped nginx site and DEMO app on private loopback ports and disposable data."""
import base64
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/linux"
DOMAIN = "demo.example.invalid"
ORIGIN = f"https://{DOMAIN}"


def test_proxy_and_service_contract():
    site = (DEPLOY / "nginx-demo.conf").read_text()
    for header in ("Authorization", "Proxy-Authorization", "Forwarded", "X-Forwarded-Host",
                   "X-Forwarded-Port", "X-Forwarded-By"):
        assert f'proxy_set_header {header} "";' in site
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in site
    assert "proxy_set_header X-Forwarded-Proto https;" in site
    assert "proxy_set_header Host $http_host;" in site
    unit = (DEPLOY / "maison-demo.service").read_text()
    assert "User=maison-demo" in unit and "Group=maison-demo" in unit
    assert "UMask=0077" in unit and "ReadWritePaths=/var/lib/maison-demo" in unit
    assert "Restart=on-failure" in unit and "WantedBy=multi-user.target" in unit
    assert "ExecStartPre=/opt/maison-demo/.venv/bin/python /opt/maison-demo/tools/check_sqlite_runtime.py --require-fixed" in unit


def unused_ports(count):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]


@contextmanager
def listen(command, port, output):
    with output.open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail(f"{Path(command[0]).name} did not start: {output.read_text()}")
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def request(port, path, *, tls=None, sni=DOMAIN, headers=None, data=None, method=None):
    with closing(http.client.HTTPConnection(DOMAIN, timeout=5)) as conn:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sock = tls.wrap_socket(sock, server_hostname=sni) if tls else sock
        conn.request(method or ("POST" if data is not None else "GET"), path,
                     json.dumps(data) if data is not None else None, headers or {})
        response = conn.getresponse()
        return response.status, response.headers, response.read()


def nginx_command(tmp, binary, http_port, https_port, upstream_port):
    site = (DEPLOY / "nginx-demo.conf").read_text()
    # Bind the same virtual hosts to unprivileged, IPv4 loopback test sockets.
    site = re.sub(r"^\s*listen \[::\]:[^;]+;", "", site, flags=re.MULTILINE)
    for external, local in ((80, http_port), (443, https_port)):
        site = site.replace(f"listen {external} ", f"listen 127.0.0.1:{local} ")
    site = site.replace("127.0.0.1:8875", f"127.0.0.1:{upstream_port}")
    for installed, local in ((f"/etc/letsencrypt/live/{DOMAIN}/fullchain.pem", "tls.crt"),
                             (f"/etc/letsencrypt/live/{DOMAIN}/privkey.pem", "tls.key"),
                             ("/var/log/nginx/maison-demo-access.log", "access.log"),
                             ("/etc/maison-demo/gate.htpasswd", "gate.htpasswd")):
        site = site.replace(installed, f'"{tmp / local}"')
    # Single process keeps the same uid as pytest, including in rootless CI.
    prefix = f'pid "{tmp / "nginx.pid"}";\nmaster_process off;\ndaemon off;\nevents {{}}\nhttp {{\n'
    for module in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        prefix += f'{module}_temp_path "{tmp / module}";\n'
    config = tmp / "nginx.conf"
    config.write_text(prefix + site + "}\n")
    command = [binary, "-e", str(tmp / "error.log"), "-p", str(tmp), "-c", str(config)]
    subprocess.run([*command, "-t"], check=True, capture_output=True)
    return command


def test_tls_gate_and_stamp_flow(tmp_path, monkeypatch):
    nginx, openssl = shutil.which("nginx"), shutil.which("openssl")
    htpasswd = shutil.which("htpasswd")
    if not nginx or not openssl or not htpasswd:
        if os.environ.get("MAISON_HTTPS_TEST") == "1":
            pytest.fail("Explicit HTTPS integration requires nginx, openssl and htpasswd in PATH")
        pytest.skip("nginx, openssl and htpasswd are required for the loopback HTTPS integration")

    monkeypatch.syspath_prepend(str(ROOT / "app"))
    monkeypatch.syspath_prepend(str(DEPLOY))
    from demo import prepare
    from staff_admin import write_staff_password

    event = json.loads((ROOT / "app/event.json").read_text())
    now = datetime.now(timezone.utc)
    event.update(id="demo-edge-fixture", draft=False, starts_at=(now - timedelta(days=1)).isoformat(),
                 ends_at=(now + timedelta(days=1)).isoformat(), reward_until=(now + timedelta(days=2)).isoformat())
    config = tmp_path / "event.json"
    config.write_text(json.dumps(event))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    prepare(state, config, ORIGIN)
    database = state / "collection.sqlite"
    staff_password = secrets.token_urlsafe(24)
    write_staff_password(database, "edge-fixture", staff_password)
    with closing(sqlite3.connect(database)) as db:
        qr = db.execute("SELECT qr_key FROM products ORDER BY id LIMIT 1").fetchone()[0]

    gate_user, gate_password = "demo-fixture", secrets.token_urlsafe(24)
    subprocess.run([htpasswd, "-ciB", str(tmp_path / "gate.htpasswd"), gate_user],
                   input=gate_password + "\n", check=True, capture_output=True, text=True)
    authorization = "Basic " + base64.b64encode(f"{gate_user}:{gate_password}".encode()).decode()
    cert = tmp_path / "tls.crt"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={DOMAIN}", "-addext", f"subjectAltName=DNS:{DOMAIN}",
                    "-out", str(cert), "-keyout", str(tmp_path / "tls.key")], check=True, capture_output=True)
    tls = ssl.create_default_context(cafile=str(cert))
    http_port, https_port, app_port = unused_ports(3)
    nginx_cmd = nginx_command(tmp_path, nginx, http_port, https_port, app_port)
    app_cmd = [sys.executable, str(DEPLOY / "demo.py"), "serve", "--state-dir", str(state),
               "--config", str(config), "--origin", ORIGIN, "--port", str(app_port)]
    name, phone = "ProxyFixtureName", "01000005555"
    from demo import contact, load_demo_event, migrate_contacts
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from contact_fixture import write_config
    policy = write_config(contact, tmp_path / "synthetic.json", load_demo_event(config)["id"], [(name, phone)])
    migrate_contacts(state, config, ORIGIN, policy)
    app_cmd.extend(["--synthetic-config", str(policy)])
    gate = {"Authorization": authorization}
    post = {**gate, "Origin": ORIGIN, "Content-Type": "application/json", "X-Stamp-Client": "1"}

    with ExitStack() as stack:
        stack.enter_context(listen(app_cmd, app_port, tmp_path / "app.log"))
        stack.enter_context(listen(nginx_cmd, https_port, tmp_path / "nginx.log"))

        def https(path, **options):
            return request(https_port, path, tls=tls, **options)

        code, headers, _ = request(http_port, "/")
        assert code == 404 and "WWW-Authenticate" not in headers
        # Keep CA verification, but let the server itself decide SNI rejection.
        foreign_sni = ssl.create_default_context(cafile=str(cert))
        foreign_sni.check_hostname = False
        with pytest.raises(ssl.SSLError):
            request(https_port, "/health", tls=foreign_sni, sni="other.example.invalid")
        for host in ("other.example.invalid", DOMAIN + ":9000"):
            assert https("/health", headers={**gate, "Host": host})[0] == 421
        for path in ("/", "/admin", "/recover", "/health", "/static/app.js", f"/q/{qr}", "/api/state"):
            code, headers, _ = https(path)
            assert code == 401 and headers["WWW-Authenticate"].startswith("Basic ")
            assert headers["Cache-Control"] == "no-store"
            assert headers["Referrer-Policy"] == "no-referrer"
            assert headers["Strict-Transport-Security"].startswith("max-age=")
        for method in ("HEAD", "OPTIONS"):
            assert https("/admin", method=method)[0] == 401
        assert https("/api/register", data={})[0] == 401
        assert https("/?private=query-fixture", headers={"Authorization": "Basic eDp4"})[0] == 401
        assert https("/health", headers={**gate, "X-Forwarded-Proto": "http",
                                         "Forwarded": "proto=http", "X-Forwarded-Host": "other.invalid"})[0] == 200

        profile = {"name": name, "phone": phone, "notice_acknowledged": True, "notice_version": "synthetic-contact-v1"}
        assert https("/api/register", headers={**post, "Origin": "https://other.invalid"}, data=profile)[0] == 403
        code, headers, _ = https("/api/register", headers=post, data=profile)
        assert code == 200
        customer = headers["Set-Cookie"].split(";", 1)[0]
        assert "Domain=" not in headers["Set-Cookie"]
        assert customer.startswith("__Host-collection=")
        assert all(flag in headers["Set-Cookie"] for flag in ("Secure", "HttpOnly", "Path=/", "SameSite=Lax"))
        visitor = {**post, "Cookie": customer}
        assert https("/api/admin/stats", headers=visitor)[0] == 401
        assert https(f"/q/{qr}", headers=visitor)[0] == 200
        code, _, data = https("/api/state", headers=visitor)
        assert code == 200 and json.loads(data)["customer"]["count"] == 0
        collect = {**visitor, "X-CSRF-Token": json.loads(data)["csrf"]}
        for expected in (True, False):
            code, _, data = https("/api/collect", headers=collect, data={"qr": qr})
            assert code == 200 and json.loads(data)["added"] is expected

        code, headers, _ = https("/api/find", headers=post, data={"name": name, "phone": phone})
        assert code == 200
        second_customer = headers["Set-Cookie"].split(";", 1)[0]
        assert second_customer != customer
        result = https("/api/state", headers={**gate, "Cookie": second_customer})
        assert result[0] == 200 and json.loads(result[2])["customer"]["count"] == 1

        code, headers, _ = https("/api/admin/login", headers=post, data={"password": staff_password})
        assert code == 200
        staff = headers["Set-Cookie"].split(";", 1)[0]
        assert "Domain=" not in headers["Set-Cookie"]
        assert staff.startswith("__Host-stamp-admin=")
        assert all(flag in headers["Set-Cookie"] for flag in ("Secure", "HttpOnly", "Path=/", "SameSite=Strict"))
        assert https("/api/admin/stats", headers={"Cookie": staff})[0] == 401
        code, _, data = https("/api/admin/stats", headers={**gate, "Cookie": staff})
        assert code == 200 and json.loads(data)["stamps"] == 1
        assert json.loads(https("/api/state", headers={**gate, "Cookie": staff})[2])["customer"] is None

    access = (tmp_path / "access.log").read_text().splitlines()
    assert len(access) >= 20
    assert all(re.fullmatch(r"[0-9T:+-]+ [1-5]\d\d \d+", line) for line in access)
    for log in tmp_path.glob("*.log"):
        content = log.read_text()
        for private in (qr, name, phone, customer, second_customer, staff, staff_password, authorization, gate_user,
                        gate_password, "query-fixture"):
            assert private not in content, f"Sensitive field appeared in {log.name}"
