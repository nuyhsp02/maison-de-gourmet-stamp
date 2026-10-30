"""Guard tracked files/history against runtime data and common secret patterns.

This is a bounded publication check, not a complete security audit.
Never prints matching secret values. Only standard-library dependencies.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    ('private-key', re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')),
    ('github-token', re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})')),
    ('aws-access-key', re.compile(rb'(?:AKIA|ASIA)[A-Z0-9]{16}')),
    ('cloudflare-tunnel-token', re.compile(rb'eyJ[A-Za-z0-9_+/=-]{70,}')),
    ('local-user-path', re.compile(rb'(?:/' + rb'Users/|/ho' + rb'me/(?!runner/)|[A-Za-z]:\\{1,2}Users\\{1,2})')),
]
QR_LITERAL = re.compile(rb'/q/([A-Za-z0-9_-]{32})(?![A-Za-z0-9_-])')
PRIVATE_DIRS = {'data', 'backups', 'logs', '.venv', '.test-venv', 'venv', 'qr-export', 'qr-exports', '__pycache__', 'work', 'node_modules'}
PRIVATE_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.backup', '.bak', '.pem', '.key', '.pfx', '.p12', '.crt', '.cer', '.pyc', '.zip', '.exe', '.dll'}


def git(*arguments, root=ROOT):
    return subprocess.run(['git', '-C', str(root), *arguments], check=True, stdout=subprocess.PIPE).stdout


def inspect_blob(name, data):
    path = PurePosixPath(name)
    findings = []
    if path.is_absolute() or '..' in path.parts:
        findings.append('unsafe-path')
    if set(path.parts) & PRIVATE_DIRS or path.suffix.lower() in PRIVATE_SUFFIXES or any(
            path.name.endswith(suffix) for suffix in ('-wal', '-shm', '-journal')):
        findings.append('runtime-or-binary-file')
    if path.parts and path.parts[0] == 'config' and name not in ('config/event.example.json', 'config/operations.example.json'):
        findings.append('private-config')
    if path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example' or path.name == 'tunnel-token.txt':
        findings.append('credential-file')
    if data.startswith(b'SQLite format 3\0'):
        findings.append('sqlite-content')
    for kind, pattern in PATTERNS:
        if pattern.search(data):
            findings.append(kind)
    for match in QR_LITERAL.finditer(data):
        # Existing decoder fixtures intentionally contain a*32 / b*32 and only in tests.
        if not (name.startswith('app/tests/') and len(set(match[1])) == 1):
            findings.append('literal-product-qr')
    if name in ('app/event.json', 'config/event.example.json'):
        try:
            event = json.loads(data)
            if event.get('draft') is not True or any(event.get(k) is not None for k in ('starts_at', 'ends_at', 'reward_until')):
                findings.append('non-example-event')
        except (ValueError, UnicodeError):
            findings.append('invalid-event-example')
    if name == 'config/operations.example.json':
        try:
            cfg = json.loads(data)
            if cfg.get('PublicUrl') != '' or cfg.get('ExternalBackupPaths') != []:
                findings.append('non-example-operations')
        except (ValueError, UnicodeError):
            findings.append('invalid-operations-example')
    return sorted(set(findings))


def audit(root=ROOT, history=False, staged=False):
    records = []
    if staged:
        paths = git('ls-files', '--cached', '-z', root=root).decode().split('\0')
        for name in filter(None, paths):
            records.append(('index', name, git('show', ':' + name, root=root)))
    else:
        tree = git('ls-tree', '-r', '-z', 'HEAD', root=root).split(b'\0')
        for record in filter(None, tree):
            meta, name = record.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            if mode in ('120000', '160000') or kind != 'blob':
                records.append((oid, name.decode(), b'SQLite format 3\0'))  # Reject links/submodules.
            else:
                records.append((oid, name.decode(), git('cat-file', 'blob', oid, root=root)))
    commits = 0
    if history:
        seen = {(oid, name) for oid, name, _ in records}
        for commit in git('rev-list', '--all', root=root).decode().splitlines():
            commits += 1
            records.append((commit, '(commit metadata)', git('cat-file', 'commit', commit, root=root)))
            for record in filter(None, git('ls-tree', '-r', '-z', commit, root=root).split(b'\0')):
                meta, raw_name = record.split(b'\t', 1)
                mode, kind, oid = meta.decode().split()
                name = raw_name.decode()
                if (oid, name) in seen: continue
                seen.add((oid, name))
                content = git('cat-file', 'blob', oid, root=root) if kind == 'blob' and mode not in ('120000', '160000') else b'SQLite format 3\0'
                records.append((oid, name, content))
    if history:
        for tag in git('for-each-ref', '--format=%(objecttype) %(objectname)', 'refs/tags', root=root).decode().splitlines():
            kind, oid = tag.split()
            if kind == 'tag':
                records.append((oid, '(annotated tag metadata)', git('cat-file', 'tag', oid, root=root)))
    findings = [{'file': name, 'object': oid[:12], 'issues': issues}
                for oid, name, data in records if (issues := inspect_blob(name, data))]
    return {'result': 'fail' if findings else 'pass', 'objects_checked': len(records),
            'commits_checked': commits, 'findings': findings,
            'scope': 'tracked/index and optional reachable history; pattern scan, no guarantee of complete secret detection'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    result = audit(history=args.history, staged=args.staged)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['result'] != 'pass'


if __name__ == '__main__':
    raise SystemExit(main())
