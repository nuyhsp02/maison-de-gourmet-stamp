import json
import subprocess
import sys
from pathlib import Path

from app import ROOT, connect_db
from conftest import Visitor


def test_export_fixed_qr_and_live_backup(app, tmp_path):
    def command(*args):
        return subprocess.run([sys.executable, str(ROOT / "manage.py"), "--database",
                               app.config["DATABASE"], *args], capture_output=True, text=True)
    out = tmp_path / "qr"
    args = ("qr-export", "--public-url", "http://127.0.0.1:8765", "--out", str(out))
    assert command(*args).returncode == 0
    manifest = (out / "manifest.json").read_text()
    assert len(json.loads(manifest)) == 10
    assert len(list(out.glob("*.svg"))) == 10
    assert "viewBox=" in next(out.glob("*.svg")).read_text()
    assert command(*args).returncode == 0
    assert (out / "manifest.json").read_text() == manifest
    Visitor(app)
    target = tmp_path / "safe.backup"
    assert command("backup", str(target)).returncode == 0
    with connect_db(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert command("backup", str(target)).returncode != 0
