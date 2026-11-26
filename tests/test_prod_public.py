"""Public HTTPS ingress, isolated assets and audited contact viewing on disposable PROD-shaped state."""
from contextlib import ExitStack, closing
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import zipfile

import pytest

from lifecycle_fixtures import authority_for, before_privacy_cutoff, provision_local_controls
from nacl.public import PrivateKey
from prod_fixtures import ROOT, make_config, prod
from test_linux_proxy import listen, request, unused_ports

sys.path.insert(0, str(ROOT / "tools"))
import build_prod_package
import contact_mac
import prod_contact_ssh as operator
from staff_admin import read_credentials, staff_key_path, write_staff_password

pytestmark = pytest.mark.usefixtures("before_privacy_cutoff")

DEPLOY = ROOT / "deploy/linux"
HOST = "gourmetstamp.kr"
ORIGIN = f"https://{HOST}"


def test_prod_package_has_only_runtime_assets_and_matches_static_routes(tmp_path):
    data = build_prod_package.package(lambda name: (ROOT / name).read_bytes(), "synthetic")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        files = json.loads(archive.read("PROD_MANIFEST.json"))["sha256"]
        assert set(files) == names - {"PROD_MANIFEST.json"}
        assert all(hashlib.sha256(archive.read(n)).hexdigest() == digest for n, digest in files.items())
        static = {p.removeprefix("app/static/") for p in names if p.startswith("app/static/")}
        assert static == set(prod.application.PROD_STATIC_FILES) | {"vendor/jsQR.LICENSE"}
        assert len(list((ROOT / "app/static/stamps").glob("*.png"))) == 10
        assert not any("demo" in n or n.endswith((".png", ".key", ".sqlite", ".cms")) for n in names)
        required = {"prod_local_lifecycle.py", "prod_custody.py", "prod_restore_approval.py",
                    "prod_object_backup.py", "prod_s3.py", "prod_backup_source.py", "maison-prod.service",
                    "maison-prod-backup-source.socket", "maison-prod-backup-source@.service",
                    "maison-prod-backup.service", "maison-prod-backup.timer",
                    "maison-prod-purge.service", "maison-prod-purge.timer"}
        assert {"deploy/linux/" + name for name in required} <= names
        assert {"tools/contact_mac.py", "deploy/linux/prod_authority.py"}.isdisjoint(names)
        archive.extractall(tmp_path)
    # Every server command must import and parse its parameters from the package alone.
    for script in ("deploy/linux/prod.py", "deploy/linux/prod_backup.py",
                   "deploy/linux/prod_contact_ssh.py", "deploy/linux/prod_local_lifecycle.py",
                   "deploy/linux/prod_object_backup.py", "deploy/linux/prod_backup_custody.py",
                   "app/manage.py"):
        result = subprocess.run([sys.executable, "-B", script, "--help"], cwd=tmp_path, capture_output=True)
        assert result.returncode == 0, (script, result.stderr)


def test_production_site_and_unit_contract():
    text = (DEPLOY / "nginx-prod.conf").read_text()
    assert "auth_basic off;" in text and "auth_basic_user_file" not in text and "htpasswd" not in text
    assert "auth_basic_user_file" in (DEPLOY / "nginx-demo.conf").read_text()
    unit = (DEPLOY / "maison-prod.service").read_text()
    assert "User=maison-prod" in unit and "UMask=0077" in unit and "prod.py serve" in unit
    assert "--policy" not in unit and "prod.py prepare" not in unit


@pytest.mark.skipif(os.name != "posix", reason="Designated Mac only")
def test_contact_key_backup_requires_detached_owner_only_output(tmp_path):
    private = tmp_path.resolve() / "custody"
    private.mkdir(mode=0o700)
    output = private / "new-copy"
    contact_mac.custody_output(output)
    private.chmod(0o755)
    with pytest.raises(ValueError):
        contact_mac.custody_output(output)
    private.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(private)
    with pytest.raises(ValueError):
        contact_mac.custody_output(alias / "new-copy")
    (private / ".git").touch()
    with pytest.raises(ValueError):
        contact_mac.custody_output(output)


@pytest.fixture
def instance(tmp_path):
    if os.name != "posix":
        pytest.skip("Linux private filesystem contract")
    folder = tmp_path.resolve()
    key = PrivateKey.generate()
    config, descriptor = make_config(folder, key.public_key, origin=ORIGIN)
    event = prod.load_prod_event(config)
    recipient = prod.contact.ProductionEnrollment.read(descriptor, event["id"])
    state = folder / "state"
    state.mkdir(mode=0o700)
    db = prod.prepare(state, config, ORIGIN, recipient)
    return folder, state, db, config, descriptor, event, recipient, key


def seed_card(db, recipient, name, phone):
    cid = secrets.token_hex(16)
    key = prod.application.default_profile_key_path(db).read_bytes()
    lookup = hmac.digest(key, phone.encode(), "sha256").hex()
    with closing(sqlite3.connect(db)) as writer, writer:
        writer.execute("INSERT INTO customers VALUES (?,1,NULL)", (cid,))
        writer.execute("INSERT INTO customer_profiles VALUES (?,?,?,'prod-fixture',1)", (cid, name, lookup))
        recipient.store(writer, cid, phone, 1)
    return cid


def test_prod_operator_only_releases_after_audit_and_decrypts_bound_payload(instance):
    _, state, database, config, _, event, recipient, key = instance
    access = prod.PrivacyAccess(state, event, recipient, authority_for(state, config))
    digits = "010" + "00000001"
    cid = seed_card(database, recipient, "Synthetic pickup", digits)
    req = {"customer_id": cid, "purpose": "pickup"}
    with closing(prod.application.connect_db(database, mode="rw")) as db:
        kp = prod.application.default_profile_key_path(database)
        reply = operator.release(db, event, recipient, kp, "fixture-operator", req, access=access)
        assert digits not in json.dumps(reply)
        assert contact_mac.open_phone(key, reply, event["id"], cid).replace("-", "") == digits
        operator.acknowledge(db, "fixture-operator", reply["request_id"], "displayed", access=access)
        assert db.execute("SELECT result FROM contact_access_audit").fetchone()[0] == "displayed"
        with pytest.raises(ValueError):
            operator.release(db, event, recipient, kp, "fixture-operator", {**req, "purpose":"marketing"}, access=access)
        db.set_authorizer(lambda action, arg, *rest: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT
                          and arg == "contact_access_audit" else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            operator.release(db, event, recipient, kp, "fixture-operator", req, access=access)


def test_legacy_and_prod_credential_files_are_not_interchangeable(instance):
    tmp, state, database, config, descriptor, event, recipient, _ = instance
    password = secrets.token_urlsafe(24)
    with pytest.raises(ValueError):
        write_staff_password(database, "fixture-counter", password)
    write_staff_password(database, "fixture-counter", password, production=True)
    assert read_credentials(staff_key_path(database), private=True) is None
    write_staff_password(database, "fixture-manager", secrets.token_urlsafe(24), role="admin", production=True)
    prod.check(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    # A file valid for the legacy staff reader does not satisfy the active PROD binding.
    legacy = tmp / "other" / "collection.sqlite"
    write_staff_password(legacy, "fixture-legacy", secrets.token_urlsafe(24))
    verifier = staff_key_path(legacy)
    assert read_credentials(verifier, private=True) is not None
    staff_key_path(database).write_bytes(verifier.read_bytes())
    with pytest.raises(ValueError):
        prod.check(state, config, ORIGIN, recipient, authority=authority_for(state, config))
    with pytest.raises(ValueError):
        write_staff_password(legacy, "fixture-counter", password, production=True)


def prod_proxy(tmp, ports):
    http_port, https_port, upstream, inherited_port = ports
    site = (DEPLOY / "nginx-prod.conf").read_text()
    site = re.sub(r"^\s*listen \[::\]:[^;]+;", "", site, flags=re.MULTILINE)
    for external, local in [(80, http_port), (443, https_port)]:
        site = re.sub(rf"listen {external}(?=[ ;])", f"listen 127.0.0.1:{local}", site)
    for installed, actual in [("127.0.0.1:8876", f"127.0.0.1:{upstream}"),
                              (f"/etc/letsencrypt/live/{HOST}/fullchain.pem", str(tmp / "tls.crt")),
                              (f"/etc/letsencrypt/live/{HOST}/privkey.pem", str(tmp / "tls.key")),
                              ("/var/log/nginx/maison-prod-access.log", str(tmp / "access.log"))]:
        site = site.replace(installed, actual)
    # A parent challenge would apply to the public site if it inherited this setting.
    top = f'pid "{tmp}/nginx.pid"; master_process off; daemon off; events {{}} http {{\n'
    (tmp / "parent-users").touch(mode=0o600)
    top += f'access_log off; auth_basic "inherited-test"; auth_basic_user_file "{tmp}/parent-users";\n'
    # The existing shared server owns both default listeners.
    top += f'server {{ listen 127.0.0.1:{http_port} default_server; return 404; }}\n'
    top += (f'server {{ listen 127.0.0.1:{https_port} ssl default_server; '
            'ssl_reject_handshake on; return 421; }\n')
    # Same parent and upstream. This control server must challenge a guest.
    top += (f'server {{ listen 127.0.0.1:{inherited_port}; '
            f'location / {{ proxy_pass http://127.0.0.1:{upstream}; }} }}\n')
    for module in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        top += f'{module}_temp_path "{tmp}/{module}";\n'
    config = tmp / "nginx.conf"
    config.write_text(top + site + "}\n", encoding="utf-8")
    cmd = [shutil.which("nginx"), "-e", str(tmp / "error.log"), "-p", str(tmp), "-c", str(config)]
    subprocess.run([*cmd, "-t"], capture_output=True, check=True)
    return cmd


def test_public_tls_without_gate_and_closed_enrollment(instance, monkeypatch):
    tmp, state, database, config, descriptor, event, recipient, _ = instance
    if not shutil.which("nginx") or not shutil.which("openssl"):
        if os.environ.get("MAISON_HTTPS_TEST") == "1":
            pytest.fail("Explicit HTTPS integration requires nginx and openssl")
        pytest.skip("Local nginx/openssl required")
    role_pw = {role: secrets.token_urlsafe(24) for role in ("staff", "admin")}
    for role, pwd in role_pw.items():
        write_staff_password(database, "fixture-" + role, pwd, role=role, production=True)
    name, phone = "Fixture public customer", "010" + "00000002"
    seed_card(database, recipient, name, phone)
    with closing(sqlite3.connect(database)) as db:
        qr = db.execute("SELECT qr_key FROM products LIMIT 1").fetchone()[0]
    cert = tmp / "tls.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={HOST}", "-addext", f"subjectAltName=DNS:{HOST},DNS:www.{HOST}",
                    "-out", str(cert), "-keyout", str(tmp / "tls.key")], capture_output=True, check=True)
    tls = ssl.create_default_context(cafile=str(cert))
    ports = unused_ports(4)
    nginx = prod_proxy(tmp, ports)
    serve = [sys.executable, str(ROOT / "tests/lifecycle_fixtures.py"), "serve", "--state-dir", str(state), "--config", str(config),
             "--recipient", str(descriptor), "--origin", ORIGIN, "--port", str(ports[2])]
    def https(path, *, headers=None, data=None):
        result = request(ports[1], path, tls=tls, sni=HOST, headers={"Host":HOST, **(headers or {})}, data=data)
        assert "WWW-Authenticate" not in result[1]
        return result
    post = {"Origin": ORIGIN, "Content-Type":"application/json", "X-Stamp-Client":"1"}
    with ExitStack() as servers:
        # Launch the real PROD command with local approval and state only. The custodian and
        # operator Mac have no running process or network descriptor in this integration.
        provision_local_controls(tmp, state, config, monkeypatch)
        servers.enter_context(listen(serve, ports[2], tmp / "app.log"))
        servers.enter_context(listen(nginx, ports[1], tmp / "nginx.log"))
        code, headers, _ = request(ports[3], "/", headers={"Host":HOST})
        assert code == 401 and headers["WWW-Authenticate"].startswith("Basic ")
        code, headers, _ = request(ports[0], "/q/" + qr, headers={"Host":HOST})
        assert code == 308 and headers["Location"] == ORIGIN + "/q/" + qr and "WWW-Authenticate" not in headers
        path = "/q/" + qr + "?source=fixture"
        for port, context in ((ports[0], None), (ports[1], tls)):
            code, headers, _ = request(port, path, tls=context, sni="www." + HOST,
                                       headers={"Host": "www." + HOST})
            assert code == 308 and headers["Location"] == ORIGIN + path
            assert "WWW-Authenticate" not in headers
        foreign = ssl.create_default_context(cafile=str(cert))
        foreign.check_hostname = False
        with pytest.raises(ssl.SSLError):
            request(ports[1], "/", tls=foreign, sni="other.example.invalid")
        for host in ("other.example.invalid", HOST + ":9000"):
            assert https("/", headers={"Host":host})[0] == 421
        for path in ("/", "/q/" + qr, "/static/style.css", "/static/favicon.svg", "/static/app.js", "/api/state"):
            assert https(path)[0] == 200
        assert https("/static/stamps/cookie.png")[0] == 404
        code, _, body = https("/api/register", headers=post, data={"name":name,"phone":phone})
        assert code == 403 and json.loads(body)["code"] == "registration_closed"
        assert https("/api/find", headers=post, data={"name":"other","phone":phone})[0] == 404
        code, headers, _ = https("/api/find", headers=post, data={"name":name,"phone":phone})
        assert code == 200 and "Secure" in headers["Set-Cookie"]
        customer = headers["Set-Cookie"].split(";", 1)[0]
        assert https("/api/admin/stats", headers={"Cookie":customer})[0] == 401
        assert https("/api/admin/correct", headers=post, data={})[0] == 401
        for role, route in (("staff","login"), ("admin","manager-login")):
            code, headers, body = https("/api/admin/" + route, headers=post, data={"password":role_pw[role]})
            assert code == 200 and json.loads(body)["role"] == role
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            assert https("/api/admin/stats", headers={"Cookie":cookie})[0] == 200
            code, _, body = https("/api/admin/correct", headers={**post,"Cookie":cookie}, data={})
            assert code == 403 and json.loads(body)["code"] == ("admin_required" if role == "staff" else "admin_csrf_failed")
    for log in tmp.glob("*.log"):
        raw = log.read_text()
        assert all(secret not in raw for secret in (qr, phone, name, customer, *role_pw.values()))
    access = (tmp / "access.log").read_text().splitlines()
    assert len(access) >= 16 and all(re.fullmatch(r"[0-9T:+-]+ [1-5]\d\d \d+", line) for line in access)
