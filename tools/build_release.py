"""Build a Windows release only from one reviewed, committed Git tree.

No runtime directory is copied. All archived files pass the publication guard.
The deployment layout contract remains version 1.4; the Git release has its own tag.
"""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from audit_public import inspect_blob

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, stdout=subprocess.PIPE).stdout


def build(root, output, ref='HEAD', version='v1.4.0'):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?', version):
        raise ValueError('Use a concrete semantic version such as v1.4.0')
    commit = git(root, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    stamp = int(git(root, 'show', '-s', '--format=%ct', commit).decode())
    date = datetime.fromtimestamp(stamp, timezone.utc)
    date_tuple = (max(1980, date.year), date.month, date.day, date.hour, date.minute, date.second)
    records = git(root, 'ls-tree', '-r', '-z', commit).split(b'\0')
    content = {}
    for record in filter(None, records):
        meta, raw_name = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode()
        path = PurePosixPath(name)
        if mode in ('120000', '160000') or kind != 'blob' or path.is_absolute() or '..' in path.parts:
            raise ValueError('Unsupported archive entry: ' + name)
        if name == 'PACKAGE_MANIFEST.json':
            raise ValueError('Do not track a generated deployment manifest')
        data = git(root, 'cat-file', 'blob', oid)
        if inspect_blob(name, data):
            raise ValueError('Public source guard rejected: ' + name)
        content[name] = data
    required = {'app/app.py', 'app/schema.sql', 'app/event.json', 'scripts/Install.ps1',
                'scripts/Update.ps1', 'scripts/ops.py', 'config/event.example.json',
                'config/operations.example.json', 'requirements-windows.lock', 'Install.cmd', 'Manage.cmd'}
    if not required <= content.keys():
        raise ValueError('Incomplete Windows package source')
    manifest = {'format': 'maison-windows-v1', 'version': '1.4', 'release_tag': version,
                'source_commit': commit, 'sha256': {name: hashlib.sha256(data).hexdigest()
                                                 for name, data in sorted(content.items())}}
    content['PACKAGE_MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / ('maison-de-gourmet-' + version + '-windows.zip')
    checksum = destination.with_suffix('.sha256.txt')
    if destination.exists() or checksum.exists():
        raise FileExistsError('Never overwrite an existing release asset')
    created_archive = created_checksum = False
    try:
        with destination.open('xb') as stream:
            created_archive = True
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, data in sorted(content.items()):
                    info = zipfile.ZipInfo('maison-cafe-v1.4/' + name, date_tuple)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip():
                raise ValueError('Archive verification failed')
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        with checksum.open('x', encoding='utf-8') as stream:
            created_checksum = True
            stream.write(digest + '  ' + destination.name + '\n')
    except Exception:
        if created_archive: destination.unlink(missing_ok=True)
        if created_checksum: checksum.unlink(missing_ok=True)
        raise
    return {'zip': str(destination), 'checksum': str(checksum), 'sha256': digest,
            'source_commit': commit, 'release_tag': version, 'files': len(content)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--version', required=True)
    parser.add_argument('--out', type=Path, default=ROOT/'dist')
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.out, args.ref, args.version), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
