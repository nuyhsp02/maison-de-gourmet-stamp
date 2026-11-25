"""Build a Windows release only from one reviewed, committed Git tree.

No runtime directory is copied. All archived files pass the publication guard.
The deployment layout contract remains version 1.4; the Git release has its own tag.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from audit_public import inspect_blob

ROOT = Path(__file__).resolve().parents[1]


class ReleaseError(ValueError):
    def __init__(self, code, message, entry=None):
        super().__init__(message)
        self.code = code
        self.entry = entry


class ReleaseArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message may contain a user-supplied path or ref.
        self.exit(2, 'release error: RELEASE_ARGUMENTS\n')


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def build(root, output, ref='HEAD', version='v1.4.0'):
    try:
        return _build(root, output, ref, version)
    except FileExistsError:
        # Exclusive creation may fail with an OSError containing the output path.
        raise FileExistsError('RELEASE_OUTPUT_EXISTS') from None
    except subprocess.CalledProcessError:
        raise ReleaseError('RELEASE_GIT', 'Git read failed') from None
    except OSError:
        raise ReleaseError('RELEASE_IO', 'Release I/O failed') from None


def _build(root, output, ref, version):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?', version):
        raise ReleaseError('RELEASE_VERSION', 'Invalid release version')
    commit = git(root, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    stamp = int(git(root, 'show', '-s', '--format=%ct', commit).decode())
    date = datetime.fromtimestamp(stamp, timezone.utc)
    date_tuple = (max(1980, date.year), date.month, date.day, date.hour, date.minute, date.second)
    records = git(root, 'ls-tree', '-r', '-z', commit).split(b'\0')
    content = {}
    for entry, record in enumerate(filter(None, records), start=1):
        meta, raw_name = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode()
        path = PurePosixPath(name)
        if mode in ('120000', '160000') or kind != 'blob' or path.is_absolute() or '..' in path.parts:
            raise ReleaseError('RELEASE_ENTRY', 'Unsupported archive entry', entry)
        if name == 'PACKAGE_MANIFEST.json':
            raise ReleaseError('RELEASE_MANIFEST', 'Tracked generated manifest', entry)
        data = git(root, 'cat-file', 'blob', oid)
        if inspect_blob(name, data):
            raise ReleaseError('RELEASE_SOURCE_REJECTED', 'Public source guard rejected', entry)
        content[name] = data
    required = {'app/app.py', 'app/schema.sql', 'app/event.json', 'scripts/Install.ps1',
                'scripts/Update.ps1', 'scripts/ops.py', 'config/event.example.json',
                'config/operations.example.json', 'requirements-windows.lock', 'Install.cmd', 'Manage.cmd'}
    if not required <= content.keys():
        raise ReleaseError('RELEASE_INCOMPLETE', 'Incomplete Windows package source')
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
                raise ReleaseError('RELEASE_VERIFY', 'Archive verification failed')
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
    parser = ReleaseArgumentParser()
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--version', required=True)
    parser.add_argument('--out', type=Path, default=ROOT/'dist')
    args = parser.parse_args()
    try:
        result = build(ROOT, args.out, args.ref, args.version)
    except ReleaseError as error:
        location = '' if error.entry is None else f' (tree entry {error.entry})'
        print(f'release error: {error.code}{location}', file=sys.stderr)
        return 1
    except FileExistsError:
        print('release error: RELEASE_OUTPUT_EXISTS', file=sys.stderr)
        return 1
    except Exception:
        # Third-party / platform exceptions can embed paths; never print their payload.
        print('release error: RELEASE_FAILED', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
