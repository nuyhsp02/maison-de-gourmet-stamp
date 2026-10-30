"""CI-only real PrepareOnly installer/rerun; never uses an existing installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    assert sys.platform == 'win32' and os.environ.get('GITHUB_ACTIONS') == 'true'
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='Maison315-InstallCI-', dir='C:\\') as temp:
        base = Path(temp)
        package, root = base / 'package with spaces', base / 'installed with spaces'
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=repo).decode().split('\0')
        manifest = {'format': 'maison-windows-v1', 'version': '1.4', 'sha256': {}}
        for name in filter(None, names):
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / name, target)
            manifest['sha256'][name] = sha(target)
        (package / 'PACKAGE_MANIFEST.json').write_text(json.dumps(manifest), encoding='utf-8')
        # Test staging manifest only: no ZIP, tag, release or deployment artifact.
        command = ['powershell.exe', '-NoProfile', '-File', str(package / 'scripts/Install.ps1'),
                   '-Root', str(root), '-PythonExe', args.python, '-PrepareOnly']
        subprocess.run(command, check=True)
        assert not (root / 'data/collection.sqlite').exists()
        sys.path.insert(0, str(root / 'app'))
        from app import create_app
        create_app(database=root / 'data/collection.sqlite', dev=True, testing=True)
        with closing(sqlite3.connect(root / 'data/collection.sqlite')) as db:
            db.execute("INSERT INTO customers VALUES ('ci-customer',1,NULL)")
            db.commit()
        event = root / 'config/event.json'
        shutil.copyfile(root / 'config/event.example.json', event)
        watched = [root / 'data/collection.sqlite', event, root / 'config/operations.json', root / 'config/installation.json', root / '.venv/pyvenv.cfg', root / '.venv/Scripts/python.exe']
        before = [sha(p) for p in watched]
        subprocess.run(command, check=True)
        assert before == [sha(p) for p in watched]
        with closing(sqlite3.connect(root / 'data/collection.sqlite')) as db:
            assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 10
        print('INSTALL315_SMOKE ' + json.dumps({'prepare': True, 'rerun': True,
              'database_config_and_qr_bytes_preserved': True, 'automatic_runtime_replacement': False}))


if __name__ == '__main__': main()
