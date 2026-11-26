"""Guard tracked files/history against runtime data and common secret patterns.

This is a bounded publication check, not a complete security audit.
Never prints matching secret values. Only standard-library dependencies.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zlib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
GITHUB_NOREPLY = re.compile(
    rb'(?:noreply@github\.com|(?:[0-9]+\+)?[a-z0-9][a-z0-9-]*(?:\[bot\])?@users\.noreply\.github\.com)',
    re.IGNORECASE | re.ASCII,
)
PATTERNS = [
    ('internal-project-page', re.compile(
        rb'\bpage_[0-9a-f]{32}\b|https?://(?:[a-z0-9-]+\.)*notion\.(?:so|site|com)/'
        rb'[^\s<>()]*?(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?![0-9a-f])',
        re.IGNORECASE)),
    ('basic-password-hash', re.compile(rb'\$2[aby]\$[0-9]{2}\$[./a-zA-Z0-9]{53}')),
    ('staff-password-hash', re.compile(rb'pbkdf2:sha256:[0-9]+\$[A-Za-z0-9]+\$[0-9a-f]{64}')),
    ('private-key', re.compile(rb'-----BEGIN (?:[A-Z0-9][A-Z0-9-]* )*PRIVATE KEY(?: BLOCK)?-----')),
    ('github-token', re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})')),
    ('aws-access-key', re.compile(rb'(?:AKIA|ASIA)[A-Z0-9]{16}')),
    ('cloudflare-tunnel-token', re.compile(rb'eyJ[A-Za-z0-9_+/=-]{70,}')),
    ('local-user-path', re.compile(rb'(?:/' + rb'Users/|/ho' + rb'me/(?!runner/)|[A-Za-z]:\\{1,2}Users\\{1,2})')),
    ('literal-recovery-link', re.compile(rb'/recover#[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])')),
    ('literal-session-token', re.compile(
        rb"""(?:__Host-(?:collection|stamp-admin)|(?:session|recovery)[_-]token)["']?\s*[:=]\s*["']?[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])""",
        re.IGNORECASE)),
]
MOBILE_NUMBER = re.compile(rb'(?<![a-zA-Z0-9_])(?:0|\+82[ -]?)1[016789][ -]?[0-9]{3,4}[ -]?[0-9]{4}(?![a-zA-Z0-9_])')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
QR_LITERAL = re.compile(rb'/q/([A-Za-z0-9_-]{32})(?![A-Za-z0-9_-])')
PRIVATE_DIRS = {'data', 'backups', 'logs', '.venv', '.test-venv', 'venv', 'qr-export', 'qr-exports', '__pycache__', 'work', 'node_modules'}
KEY_FILE_SUFFIXES = {'.pem', '.key', '.pfx', '.p12'}
PRIVATE_SUFFIXES = KEY_FILE_SUFFIXES | {'.sqlite', '.sqlite3', '.db', '.backup', '.bak', '.crt', '.cer', '.pyc', '.zip', '.exe', '.dll', '.cms', '.bundle', '.dump', '.tar', '.tgz'}
# tools/contact_mac.py lock_key and rewrap emit the same versioned envelope.
# Header + Argon2id salt + SecretBox nonce + MAC + encrypted Curve25519 key.
CONTACT_KEY_HEADER = b'MDC1'
CONTACT_KEY_SIZE = len(CONTACT_KEY_HEADER) + 16 + 24 + 16 + 32


def git(*arguments, root=ROOT):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(root), *arguments],
                          check=True, capture_output=True).stdout


def inspect_png(data):
    """Accept this project's reviewed image chunks, and reject hidden or trailing data."""
    if not data.startswith(PNG_SIGNATURE):
        return ['invalid-png']
    pos = len(PNG_SIGNATURE)
    has_pixels = False
    while pos + 12 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], 'big')
        kind = data[pos + 4:pos + 8]
        end = pos + 8 + size
        if end + 4 > len(data) or zlib.crc32(data[pos + 4:end]) != int.from_bytes(data[end:end + 4], 'big'):
            return ['invalid-png']
        if (pos == len(PNG_SIGNATURE)) != (kind == b'IHDR'):
            return ['invalid-png']
        if kind == b'IEND':
            return [] if size == 0 and end + 4 == len(data) and has_pixels else ['invalid-png']
        if kind not in {b'IHDR', b'IDAT', b'PLTE', b'tRNS', b'pHYs'}:
            return ['unreviewed-png-metadata']
        if (kind == b'IHDR' and size != 13) or (kind == b'pHYs' and size != 9):
            return ['invalid-png']
        if kind == b'IDAT' and size:
            has_pixels = True
        pos = end + 4
    return ['invalid-png']


def inspect_commit_identities(data):
    headers = data.partition(b'\n\n')[0].split(b'\n')
    findings = []
    for role in ('author', 'committer'):
        prefix = role.encode() + b' '
        values = [line[len(prefix):] for line in headers if line.startswith(prefix)]
        identity = None
        if len(values) == 1:
            identity = re.fullmatch(rb'[^<>\n]* <([^<>\s]+)> -?[0-9]+ [+-][0-9]{4}', values[0])
        if identity is None or not GITHUB_NOREPLY.fullmatch(identity[1]):
            findings.append(role + '-email-not-noreply')
    return findings


def sensitive_path_issues(name):
    encoded = name.encode()
    issues = [kind for kind, pattern in PATTERNS if pattern.search(encoded)]
    if QR_LITERAL.search(b'/' + encoded):
        issues.append('literal-product-qr')
    if MOBILE_NUMBER.search(encoded):
        issues.append('possible-personal-phone')
    return issues


def inspect_blob(name, data):
    path = PurePosixPath(name)
    findings = inspect_commit_identities(data) if name == '(commit metadata)' else []
    findings.extend(sensitive_path_issues(name))
    if path.is_absolute() or '..' in path.parts:
        findings.append('unsafe-path')
    if set(path.parts) & PRIVATE_DIRS or path.suffix.lower() in PRIVATE_SUFFIXES or any(
            path.name.endswith(suffix) for suffix in ('-wal', '-shm', '-journal')):
        findings.append('runtime-or-binary-file')
    if path.parts and path.parts[0] == 'config' and name not in ('config/event.example.json', 'config/operations.example.json'):
        findings.append('private-config')
    if path.name.endswith(('.htpasswd', '.env')) or path.name.startswith('.env.') and path.name != '.env.example' or path.name == 'tunnel-token.txt':
        findings.append('credential-file')
    if data.startswith(b'SQLite format 3\0'):
        findings.append('sqlite-content')
    if len(data) == CONTACT_KEY_SIZE and data.startswith(CONTACT_KEY_HEADER):
        findings.append('encrypted-contact-private-key')
    if name.lower().endswith(('.tar.gz', '.sql.gz', '.sqlite.gz', '.db.gz')):
        findings.append('runtime-or-binary-file')
    if path.suffix.lower() == '.png' or data.startswith(PNG_SIGNATURE):
        findings.extend(inspect_png(data))
    if b'\0' not in data and not name.startswith(('tests/', 'app/tests/')) and MOBILE_NUMBER.search(data):
        findings.append('possible-personal-phone')
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


def review_other_refs(root, candidate_result, private_values, expected_archive_digest=None):
    """Account for every all-ref finding before accepting a single-branch publication."""
    archive = audit(root, history=True, private_values=private_values)
    findings = archive['findings']
    if not archive['commits_checked'] or archive['result'] != ('fail' if findings else 'pass'):
        raise ValueError('incomplete all-ref report')
    reachable = set(git('rev-list', '--objects', '--no-object-names', candidate_result['candidate_commit'],
                        root=root).decode().splitlines())
    outside = True
    entries = []
    for item in findings:
        objects = git('rev-parse', '--disambiguate=' + item['object'], root=root).decode().splitlines()
        if len(objects) != 1:
            raise ValueError('cannot attribute an all-ref finding to one object')
        oid = objects[0]
        if oid in reachable:
            outside = False
        entries.append((oid, item['file'], sorted(item['issues'])))
    # Order-independent receipt for the operator's independently reviewed object/path/rule list.
    payload = json.dumps(sorted(entries), ensure_ascii=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(payload).hexdigest()
    reviewed = digest == expected_archive_digest if expected_archive_digest is not None else not findings
    return {'result': 'pass' if outside and reviewed else 'fail',
            'digest': digest, 'findings_count': len(findings), 'commits_checked': archive['commits_checked'],
            'all_findings_outside_candidate': outside, 'matches_reviewed_inventory': reviewed}


def branch_tip(root, ref):
    """Resolve only an exact local branch; never accept revision ranges or options."""
    if ref.startswith('refs/') and not ref.startswith('refs/heads/'):
        raise ValueError('--ref requires a local branch')
    full_name = ref if ref.startswith('refs/heads/') else 'refs/heads/' + ref
    valid = subprocess.run(['git', 'check-ref-format', full_name], capture_output=True)
    if valid.returncode or ref.startswith('-'):
        raise ValueError('--ref requires a valid local branch')
    result = subprocess.run(['git', '-C', str(root), 'show-ref', '--verify', '--hash', full_name], capture_output=True)
    if result.returncode:
        raise ValueError('local candidate branch not found')
    return result.stdout.decode().strip()


def audit(root=ROOT, history=False, staged=False, ref=None, private_values=()):
    if ref is not None and history:
        raise ValueError('--ref and --history select competing history scopes')
    if ref is not None and staged:
        raise ValueError('--ref and --staged have different scopes and cannot be combined')
    tip = branch_tip(root, ref) if ref is not None else 'HEAD'
    history = history or ref is not None
    if history:
        if git('rev-parse', '--is-shallow-repository', root=root).strip() != b'false':
            raise ValueError('full history requires a complete repository')
        grafts = Path(git('rev-parse', '--path-format=absolute', '--git-path', 'info/grafts', root=root).decode().strip())
        if grafts.is_file() and any(line.strip() and not line.startswith(b'#') for line in grafts.read_bytes().splitlines()):
            raise ValueError('full history requires the original commit ancestry')
    records = []
    seen_files = set()

    def add_file(oid, raw_name, mode, kind, source):
        name = raw_name.decode()
        key = (name, mode, oid)
        if key in seen_files:
            return
        seen_files.add(key)
        if mode in ('120000', '160000') or kind != 'blob':
            content = b'SQLite format 3\0'  # Reject links and submodules as before.
        else:
            content = git('cat-file', 'blob', oid, root=root)
        records.append((source, name, content))

    if staged:
        identities = []
        for role in ('author', 'committer'):
            identity = subprocess.run(['git', '-C', str(root), 'var', 'GIT_' + role.upper() + '_IDENT'],
                                      capture_output=True)
            value = identity.stdout.rstrip(b'\n') if identity.returncode == 0 else b''
            identities.append(role.encode() + b' ' + value)
        records.append(('next commit', '(commit metadata)', b'\n'.join(identities)))
        for row in filter(None, git('ls-files', '--stage', '-z', root=root).split(b'\0')):
            meta, name = row.split(b'\t', 1)
            mode, oid, stage = meta.decode().split()
            if stage != '0':
                raise ValueError('index has unresolved entries')
            add_file(oid, name, mode, 'blob', 'index')
    elif not history:
        tree = git('ls-tree', '-r', '-z', tip, root=root).split(b'\0')
        for record in filter(None, tree):
            meta, name = record.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            add_file(oid, name, mode, kind, oid)
    commits = 0
    if not history and not staged:
        head = git('rev-parse', 'HEAD', root=root).decode().strip()
        records.append((head, '(commit metadata)', git('cat-file', 'commit', head, root=root)))
        commits = 1
    if history:
        for commit in git('rev-list', tip if ref is not None else '--all', root=root).decode().splitlines():
            commits += 1
            records.append((commit, '(commit metadata)', git('cat-file', 'commit', commit, root=root)))
            for record in filter(None, git('ls-tree', '-r', '-z', commit, root=root).split(b'\0')):
                meta, raw_name = record.split(b'\t', 1)
                mode, kind, oid = meta.decode().split()
                add_file(oid, raw_name, mode, kind, oid)
    if history and ref is None:
        for tag in git('for-each-ref', '--format=%(objecttype) %(objectname)', 'refs/tags', root=root).decode().splitlines():
            kind, oid = tag.split()
            if kind == 'tag':
                records.append((oid, '(annotated tag metadata)', git('cat-file', 'tag', oid, root=root)))
    findings = []
    review = set()
    private_values = tuple(value.lower() for value in private_values)
    for oid, name, data in records:
        issues = inspect_blob(name, data)
        hidden_key_path = (PurePosixPath(name).suffix.lower() in KEY_FILE_SUFFIXES
                           or 'private-key' in issues or 'encrypted-contact-private-key' in issues)
        path_label = '(redacted path)' if hidden_key_path or sensitive_path_issues(name) else name
        if private_values:
            path_bytes = name.encode().lower()
            lower_data = data.lower()
            if any(value in path_bytes for value in private_values):
                path_label = '(redacted path)'
                issues.append('known-private-value')
            elif any(value in lower_data for value in private_values):
                issues.append('known-private-value')
        if name.startswith(('tests/', 'app/tests/')) and b'\0' not in data and MOBILE_NUMBER.search(data):
            review.add(path_label)
        if issues:
            findings.append({'file': path_label, 'object': oid[:12], 'issues': issues})
    if ref is not None:
        scope = 'reachable history of named branch'
    elif history:
        scope = 'all reachable refs and annotated tags'
    else:
        scope = 'index' if staged else 'HEAD'
    return {'result': 'fail' if findings else 'pass', 'objects_checked': len(records),
            'commits_checked': commits, 'findings': findings,
            'scope': scope, 'candidate_commit': tip if ref is not None else None,
            'private_values_checked': len(private_values), 'phone_fixture_files_to_review': sorted(review)}


def audit_for_publication(root, *, ref, expected_sha, public_base, private_reference,
                          private_values_sha256, expected_private_count, reviewed_archive_digest=None):
    if not ref or not ref.startswith('refs/heads/'):
        raise ValueError('publication requires a full local branch ref')
    tip = branch_tip(root, ref)
    if expected_sha != tip:
        raise ValueError('approved full SHA does not match the candidate branch')
    if not public_base or not re.fullmatch(r'[0-9a-f]+', public_base) or len(public_base) != len(tip):
        raise ValueError('publication requires the full previously public commit SHA')
    if git('cat-file', '-t', public_base, root=root).strip() != b'commit':
        raise ValueError('public base is not a commit')
    ancestry = subprocess.run(['git', '--no-replace-objects', '-C', str(root),
                               'merge-base', '--is-ancestor', public_base, tip], capture_output=True)
    if ancestry.returncode != 0:
        raise ValueError('could not verify public base as an ancestor of the candidate')
    if not private_reference or hashlib.sha256(private_reference).hexdigest() != private_values_sha256:
        raise ValueError('private input does not match the approved reference checksum')
    values = private_reference.splitlines()
    if not values or any(not value or value.strip() != value for value in values):
        raise ValueError('publication requires nonempty private values, one clean record per line')
    unique_count = len({value.lower() for value in values})
    if expected_private_count != unique_count or len(values) != unique_count:
        raise ValueError('private reference count is missing, incomplete, or duplicated')
    if sensitive_path_issues(ref) or any(value.lower() in ref.encode().lower() for value in values):
        raise ValueError('candidate ref is not safe for public reporting')
    if reviewed_archive_digest is not None and not re.fullmatch(r'[0-9a-f]{64}', reviewed_archive_digest):
        raise ValueError('invalid approved archive review digest')

    result = audit(root=root, ref=ref, private_values=values)
    if (result['candidate_commit'] != expected_sha or result['commits_checked'] < 1
            or result['private_values_checked'] != expected_private_count):
        raise ValueError('candidate history or private reference check could not be verified')
    result['scope'] = 'publication gate'
    result['candidate_ref'] = ref
    result['public_base'] = public_base
    if result['result'] == 'pass':
        archive = review_other_refs(root, result, values, expected_archive_digest=reviewed_archive_digest)
        result['archive_review'] = archive
        if archive['result'] != 'pass':
            result['result'] = 'fail'
    if branch_tip(root, ref) != expected_sha:
        raise ValueError('candidate branch changed during review; repeat the audit')
    return result


class AuditArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # These argparse diagnostics otherwise echo arbitrary command-line input.
        if message.startswith('unrecognized arguments:') or ': invalid int value:' in message:
            message = 'unrecognized option or invalid argument value'
        super().error(message)


class SingleApprovalValue(argparse.Action):
    """Each branch or approval parameter must have one unambiguous value."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f'{option_string} was specified more than once')
        setattr(namespace, self.dest, values)


def main():
    parser = AuditArgumentParser(prog='audit_public.py', allow_abbrev=False)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--history', action='store_true')
    scope.add_argument('--ref', action=SingleApprovalValue, help='inspect the complete ancestry of this exact local branch')
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--private-values-stdin', action='store_true',
                        help='also match known private values read one per line from standard input; values are never reported')
    parser.add_argument('--publication', action='store_true', help='require the evidence for a single-branch publication')
    parser.add_argument('--expected-sha', action=SingleApprovalValue, help='full independently approved candidate commit ID')
    parser.add_argument('--public-base', action=SingleApprovalValue, help='full approved previously public ancestor commit ID')
    parser.add_argument('--expected-private-count', action=SingleApprovalValue, type=int,
                        help='expected number of distinct protected reference values')
    parser.add_argument('--private-values-sha256', action=SingleApprovalValue,
                        help='independently approved SHA-256 of the exact protected input bytes')
    parser.add_argument('--reviewed-archive-digest', action=SingleApprovalValue,
                        help='digest of independently reviewed out-of-candidate findings')
    args = parser.parse_args()
    if args.publication:
        if (not args.ref or args.staged or not args.expected_sha or not args.public_base
                or not args.private_values_stdin or not args.private_values_sha256
                or args.expected_private_count is None or args.expected_private_count < 1):
            parser.error('--publication requires --ref, --expected-sha, --public-base, --private-values-stdin '
                         '--private-values-sha256 and a positive --expected-private-count; --staged is incompatible')
    elif any(x is not None for x in (args.expected_sha, args.public_base, args.expected_private_count,
                                      args.private_values_sha256, args.reviewed_archive_digest)):
        parser.error('publication-only evidence flags require --publication')
    try:
        private_reference = b''
        if args.private_values_stdin:
            if sys.stdin.isatty():
                parser.error('read protected values through redirected input')
            private_reference = sys.stdin.buffer.read()
            if not private_reference.strip():
                parser.error('private value input is empty')
        if args.publication:
            result = audit_for_publication(ROOT, ref=args.ref, expected_sha=args.expected_sha,
                                           public_base=args.public_base, private_reference=private_reference,
                                           private_values_sha256=args.private_values_sha256,
                                           expected_private_count=args.expected_private_count,
                                           reviewed_archive_digest=args.reviewed_archive_digest)
        else:
            values = tuple(value for value in private_reference.splitlines() if value)
            result = audit(history=args.history, staged=args.staged, ref=args.ref, private_values=values)
    except (subprocess.SubprocessError, OSError, UnicodeError):
        parser.error('audit evidence could not be read or verified')
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['result'] != 'pass'


if __name__ == '__main__':
    raise SystemExit(main())
