"""Package a single committed PROD runtime tree with an explicit list of permitted files."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

from audit_public import inspect_blob

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = (
    "app/app.py", "app/manage.py", "app/contact_privacy.py", "app/staff_admin.py", "app/correction_schema.py",
    "app/schema.sql", "app/contact_schema.sql", "app/correction_schema.sql",
    "app/templates/index.html", "app/templates/event_closed.html", "app/templates/admin.html", "app/THIRD_PARTY.md",
    "app/static/admin.css", "app/static/admin.js", "app/static/app.js", "app/static/collection-core.js",
    "app/static/favicon.svg", "app/static/phone-input.js", "app/static/style.css",
    "app/static/vendor/jsQR.js", "app/static/vendor/jsQR.LICENSE",
    "deploy/linux/prod.py", "deploy/linux/prod_backup.py", "deploy/linux/prod_contact_ssh.py",
    "deploy/linux/prod_authority.py", "deploy/linux/prod_purge.py", "deploy/linux/prod_backup_custody.py",
    "deploy/linux/maison-prod-purge.service", "deploy/linux/maison-prod-purge.timer",
    "deploy/linux/nginx-prod.conf", "deploy/linux/maison-prod.service",
    "scripts/ops.py", "tools/check_sqlite_runtime.py", "requirements-windows.lock",
)


def package(read_member, commit):
    buffer = io.BytesIO()
    digests = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in MEMBERS:
            data = read_member(name)
            if inspect_blob(name, data):
                raise ValueError("PROD package source review failed")
            archive.writestr(name, data)
            digests[name] = hashlib.sha256(data).hexdigest()
        manifest = dict(format="maison-prod-v1", source_commit=commit, sha256=digests)
        archive.writestr("PROD_MANIFEST.json", json.dumps(manifest, sort_keys=True))
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Reviewed full commit SHA")
    parser.add_argument("--output", required=True, type=Path, help="New zip outside the checkout")
    args = parser.parse_args()
    try:
        if not re.fullmatch("[0-9a-f]{40}", args.commit) or args.output.resolve().is_relative_to(ROOT):
            raise ValueError("Invalid commit or destination")
        def git(*cmd):
            return subprocess.check_output(["git", "-C", str(ROOT), *cmd], stderr=subprocess.DEVNULL)
        if git("cat-file", "-t", args.commit).strip() != b"commit":
            raise ValueError("Commit required")
        def read_member(name):
            entry = git("ls-tree", args.commit, "--", name).decode().split()
            if len(entry) != 4 or entry[:2] not in (["100644", "blob"], ["100755", "blob"]) or entry[3] != name:
                raise ValueError("Invalid committed runtime entry")
            return git("cat-file", "blob", entry[2])
        data = package(read_member, args.commit)
        # fsync before exclusive publication. A failure never replaces another build.
        with tempfile.NamedTemporaryFile(dir=args.output.parent) as pending:
            pending.write(data)
            pending.flush()
            os.fsync(pending.fileno())
            os.link(pending.name, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError):
        parser.exit(1, "Could not build the reviewed PROD package.\n")
    print("PROD source package created.")


if __name__ == "__main__":
    main()
