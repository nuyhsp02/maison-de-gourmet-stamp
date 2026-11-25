"""Publication/history guards and exact-byte Git release packaging."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import audit_public as guard
import build_release


@pytest.mark.parametrize('name,data,expected', [
    ('data/collection.sqlite', b'anything', 'runtime-or-binary-file'),
    ('gate.htpasswd', b'fixture', 'credential-file'),
    ('demo.env', b'fixture', 'credential-file'),
    ('snapshot.cms', b'fixture', 'runtime-or-binary-file'),
    ('renamed.txt', ('$2y$12$' + 'a' * 53).encode(), 'basic-password-hash'),
    ('config/event.json', b'{}', 'private-config'),
    ('safe.txt', b'SQLite format 3\0payload', 'sqlite-content'),
    ('safe.py', ('ghp_'+'x'*36).encode(), 'github-token'),
    ('safe.txt', ('-----BEGIN '+'PRIVATE KEY-----').encode(), 'private-key'),
    ('renamed.txt', ('pbkdf2:sha256:1000000$' + 's' * 16 + '$' + 'a' * 64).encode(), 'staff-password-hash'),
    ('safe.txt', ('https://example.invalid/q/'+'a'*32).encode(), 'literal-product-qr'),
])
def test_public_guard_rejects_runtime_files_and_credentials(name,data,expected):
    assert expected in guard.inspect_blob(name,data)


@pytest.mark.parametrize('filename,kind', [
    ('synthetic-owner.notes', 'encrypted-contact-private-key'),
    ('synthetic-owner.txt', 'private-key'),
    ('synthetic-owner.profile.key', 'runtime-or-binary-file'),
])
def test_key_file_findings_hide_names_and_content_from_cli(repository, filename, kind):
    marker = b'synthetic-payload-only'
    if kind == 'encrypted-contact-private-key':
        # Header, 16-byte salt, 24-byte nonce, 16-byte MAC, 32-byte sealed key.
        payload = b'MDC1' + marker.ljust(88, b'\0')
    elif kind == 'private-key':
        payload = b'-----BEGIN ' + b'ENCRYPTED PRIVATE KEY-----\n' + marker
    else:
        payload = marker.ljust(32, b'\0')
    source = repository / filename
    source.write_bytes(payload)
    commit(repository, 'synthetic key format')
    source.unlink()
    (repository / 'overview.md').write_text('Public overview')
    commit(repository, 'remove synthetic material')

    local_tools = repository / 'tools'
    local_tools.mkdir()
    script = local_tools / 'audit_public.py'
    script.write_bytes((ROOT / 'tools' / script.name).read_bytes())
    checked = subprocess.run([sys.executable, '-B', str(script), '--ref', 'main'], capture_output=True)
    assert checked.returncode == 1
    assert filename.encode() not in checked.stdout + checked.stderr
    assert marker not in checked.stdout + checked.stderr
    finding, = json.loads(checked.stdout)['findings']
    assert finding['issues'] == [kind]
    assert finding['file'] == '(redacted path)'


def test_public_contact_settings_and_illustrative_formats_remain_allowed():
    public_config = {'format': 'synthetic-contact-v1', 'event_id': 'demo-fixture',
                     'public_key': 'ab' * 32, 'allowed_profiles': []}
    assert not guard.inspect_blob('tests/recipient.json', json.dumps(public_config).encode())
    assert not guard.inspect_blob('public.txt', b'-----BEGIN PUBLIC KEY-----\nfixture')
    assert not guard.inspect_blob('notes.txt', b'MDC1 is the documented contact envelope header.')
    assert not guard.inspect_blob('tests/sample.bin', b'public-fixture'.ljust(92, b'\0'))


def test_armored_private_key_header_in_renamed_file():
    fixture = b'-----BEGIN ' + b'PGP PRIVATE KEY BLOCK-----\nfixture'
    assert 'private-key' in guard.inspect_blob('example.txt', fixture)


def test_archive_findings_need_separate_review_and_must_stay_outside(repository):
    (repository / 'card.txt').write_text('published synthetic example')
    commit(repository, 'candidate')
    candidate = guard.audit(repository, ref='main')
    git(repository, 'switch', '-c', 'private-example')
    (repository / 'removed.txt').write_text('ghp_' + 'x' * 36)
    commit(repository, 'archived example')
    unchecked = guard.review_other_refs(repository, candidate, ())
    assert unchecked['result'] == 'fail'
    assert unchecked['all_findings_outside_candidate'] and unchecked['findings_count'] == 1
    assert guard.review_other_refs(repository, candidate, (), unchecked['digest'])['result'] == 'pass'
    (repository / 'another.txt').write_text('ghp_' + 'y' * 36)
    commit(repository, 'new unreviewed example')
    changed = guard.review_other_refs(repository, candidate, (), unchecked['digest'])
    assert changed['result'] == 'fail' and not changed['matches_reviewed_inventory']
    other = guard.audit(repository, ref='private-example')
    shared = guard.review_other_refs(repository, other, (), changed['digest'])
    assert shared['result'] == 'fail' and not shared['all_findings_outside_candidate']


def test_only_synthetic_qr_decoder_fixture_is_allowed():
    value=('http://127.0.0.1:8765/q/'+'a'*32).encode()
    assert guard.inspect_blob('app/tests/fixtures/qr-matrices.json',value)==[]
    assert 'literal-product-qr' in guard.inspect_blob('docs/qr.md',value)
    assert 'non-example-event' in guard.inspect_blob('app/event.json',b'{"draft": false}')


@pytest.mark.parametrize('prefix', [
    'page_', 'https://chatgpt.com/' + 'space/page_', 'https://project.notion.' + 'site/design-',
    'https://notion.' + 'so/workspace/', 'https://app.notion.' + 'com/p/',
])
def test_internal_page_identifiers_are_redacted_even_after_link_replacement(repository, prefix):
    page = prefix + 'f' * 32
    source = repository / 'design.md'
    source.write_text(page)
    commit(repository, 'synthetic link')
    source.write_text('See the local design record.')
    commit(repository, 'replace link')
    assert guard.audit(repository)['result'] == 'pass'
    result = guard.audit(repository, history=True)
    assert result['result'] == 'fail'
    assert [f['issues'] for f in result['findings']] == [['internal-project-page']]
    assert page not in json.dumps(result)


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout


@pytest.fixture
def repository(tmp_path):
    root=tmp_path/'repo with spaces';root.mkdir()
    git(root,'init','-b','main')
    git(root,'config','user.name','Fixture')
    git(root,'config','user.email','42+portfolio-fixture@users.noreply.github.com')
    return root


def commit(root,message):
    git(root,'add','--all');git(root,'commit','-m',message)


@pytest.mark.parametrize('author,committer,roles', [
    ('author@example.invalid', 'noreply@github.com', ['author']),
    ('fixture@users.noreply.github.com', 'committer@example.invalid', ['committer']),
    ('fixture@users.noreply.github.com.example.invalid', 'fixture+noreply@example.invalid', ['author', 'committer']),
    ('42+portfolio-fixture@users.noreply.github.com', 'noreply@github.com', []),
    ('portfolio-fixture@users.noreply.github.com', '42+fixture[bot]@users.noreply.github.com', []),
])
def test_raw_commit_identity_check(repository, monkeypatch, author, committer, roles):
    (repository/'safe.txt').write_text('synthetic')
    with monkeypatch.context() as env:
        env.setenv('GIT_AUTHOR_EMAIL', author)
        env.setenv('GIT_COMMITTER_EMAIL', committer)
        index_check = guard.audit(repository, staged=True)
        assert index_check['result'] == ('fail' if roles else 'pass')
        assert author not in json.dumps(index_check) and committer not in json.dumps(index_check)
        commit(repository, 'synthetic identity fixture')
    first_commit = git(repository, 'rev-parse', 'HEAD').decode().strip()
    expected = [{'file': '(commit metadata)', 'object': first_commit[:12],
                 'issues': [role + '-email-not-noreply' for role in roles]}] if roles else []
    assert guard.audit(repository)['findings'] == expected

    git(repository, 'commit', '--allow-empty', '-m', 'second fixture')
    assert guard.audit(repository)['result'] == 'pass'
    history = guard.audit(repository, history=True)
    assert history['findings'] == expected
    assert history['result'] == ('fail' if roles else 'pass')
    assert author not in json.dumps(history) and committer not in json.dumps(history)


def test_history_detects_secret_even_after_file_deleted(repository):
    secret=repository/'removed.txt';secret.write_text('ghp_'+'x'*36)
    commit(repository,'synthetic test record')
    secret.unlink();commit(repository,'remove synthetic record')
    assert guard.audit(repository)['result']=='pass'
    result=guard.audit(repository,history=True)
    assert result['result']=='fail' and result['commits_checked']==2
    assert any('github-token' in f['issues'] for f in result['findings'])
    assert 'ghp_'+'x'*36 not in json.dumps(result)


def package_fixture(root):
    names=['app/app.py','app/schema.sql','app/event.json','scripts/Install.ps1','scripts/Update.ps1',
           'scripts/ops.py','config/event.example.json','config/operations.example.json',
           'requirements-windows.lock','Install.cmd','Manage.cmd']
    for name in names:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        if name in ('app/event.json','config/event.example.json'):
            path.write_text('{"draft": true, "starts_at": null, "ends_at": null, "reward_until": null}')
        elif name=='config/operations.example.json':
            path.write_text('{"PublicUrl":"","ExternalBackupPaths":[]}')
        elif name.endswith('.ps1'):
            path.write_bytes(b'\xef\xbb\xbf# Windows exact bytes\r\n')
        else:path.write_text('fixture only\n')
    (root/'.gitattributes').write_text('* -text\n')
    commit(root,'package fixture')


def test_release_uses_committed_tree_preserves_windows_bytes_excludes_untracked(repository,tmp_path):
    package_fixture(repository)
    original=(repository/'scripts/Install.ps1').read_bytes()
    committed_app=git(repository,'show','HEAD:app/app.py')
    (repository/'data').mkdir();(repository/'data/live.sqlite').write_bytes(b'private untracked')
    (repository/'app/app.py').write_text('uncommitted change')
    result=build_release.build(repository,tmp_path/'release',version='v1.4.0')
    with zipfile.ZipFile(result['zip']) as archive:
        names=archive.namelist()
        assert not any('live.sqlite' in n for n in names)
        assert archive.read('maison-cafe-v1.4/app/app.py')==committed_app
        assert archive.read('maison-cafe-v1.4/scripts/Install.ps1')==original
        manifest=json.loads(archive.read('maison-cafe-v1.4/PACKAGE_MANIFEST.json'))
        assert manifest['source_commit']==git(repository,'rev-parse','HEAD').decode().strip()
        for name,digest in manifest['sha256'].items():
            assert hashlib.sha256(archive.read('maison-cafe-v1.4/'+name)).hexdigest()==digest
    assert Path(result['checksum']).read_text().startswith(result['sha256'])
    with pytest.raises(FileExistsError):build_release.build(repository,tmp_path/'release',version='v1.4.0')


def test_release_rejects_private_file_in_committed_tree(repository,tmp_path):
    package_fixture(repository)
    (repository/'config/event.json').write_text('{"private":"do not publish"}')
    commit(repository,'synthetic private setting')
    with pytest.raises(ValueError,match='guard rejected'):
        build_release.build(repository,tmp_path/'bad-release',version='v1.4.0')
    assert not (tmp_path/'bad-release').exists()


def test_history_checks_annotated_tag_metadata(repository):
    (repository/'safe.txt').write_text('safe')
    commit(repository,'safe initial fixture')
    git(repository,'tag','-a','synthetic-tag','-m','ghp_'+'x'*36)
    result=guard.audit(repository,history=True)
    assert result['result']=='fail'
    assert any(f['file']=='(annotated tag metadata)' for f in result['findings'])


def test_named_branch_checks_every_parent_and_excludes_unrelated_refs(repository):
    (repository / 'public.txt').write_text('safe')
    commit(repository, 'published base')
    base = git(repository, 'rev-parse', 'HEAD').decode().strip()
    git(repository, 'branch', 'candidate', base)
    (repository / 'internal.md').write_text('page_' + 'c' * 32)
    commit(repository, 'temporary internal link')
    (repository / 'internal.md').unlink()
    commit(repository, 'remove link from tip')
    other_tip = git(repository, 'rev-parse', 'HEAD').decode().strip()
    git(repository, 'tag', '-a', 'private-archive', '-m', 'ghp_' + 'z' * 36)
    separate = guard.audit(repository, ref='refs/heads/candidate')
    assert separate['result'] == 'pass' and separate['candidate_commit'] == base
    assert separate['commits_checked'] == 1
    assert guard.audit(repository, history=True)['result'] == 'fail'

    # A clean two-parent tip still includes the deleted link in its second parent's history.
    git(repository, 'switch', 'candidate')
    git(repository, 'commit', '--allow-empty', '-m', 'candidate development')
    first_parent = git(repository, 'rev-parse', 'HEAD').decode().strip()
    tree = git(repository, 'rev-parse', 'HEAD^{tree}').decode().strip()
    merge_tip = git(repository, 'commit-tree', tree, '-p', first_parent, '-p', other_tip, '-m', 'synthetic join').decode().strip()
    git(repository, 'update-ref', 'refs/heads/candidate', merge_tip)
    result = guard.audit(repository, ref='candidate')
    assert result['commits_checked'] == 5 and result['candidate_commit'] == merge_tip
    assert [item['issues'] for item in result['findings']] == [['internal-project-page']]


@pytest.fixture
def publication_evidence(repository):
    (repository / 'public.txt').write_text('synthetic published base')
    commit(repository, 'public base')
    base = git(repository, 'rev-parse', 'HEAD').decode().strip()
    git(repository, 'commit', '--allow-empty', '-m', 'local candidate')
    tip = git(repository, 'rev-parse', 'HEAD').decode().strip()
    reference = b'fixture-protected-value\n'
    return {'ref': 'refs/heads/main', 'expected_sha': tip, 'public_base': base,
            'private_reference': reference, 'private_values_sha256': hashlib.sha256(reference).hexdigest(),
            'expected_private_count': 1}


@pytest.mark.skipif(sys.platform == 'win32' or not shutil.which('bash'), reason='Bash publishing guide')
def test_documented_gate_stops_before_transport(tmp_path):
    guide = (ROOT / 'docs' / 'GIT_GUIDE.md').read_text(encoding='utf-8')
    script = guide.split("bash <<'PUBLISH'\n", 1)[1].split('\nPUBLISH\n', 1)[0]
    # The guide runs against local stand-ins, including the transport, in an empty directory.
    mocks = r'''
git() {
    case "$1" in
        symbolic-ref) echo "$ref" ;;
        rev-parse) if [[ "$2" == HEAD ]]; then echo "$APPROVED_SHA"; else echo moved; fi ;;
        status|diff|merge-base) return 0 ;;
        bundle) if [[ "$2" == list-heads ]]; then echo "$APPROVED_SHA $ref"; fi ;;
        remote) echo "$remote" ;;
        ls-remote) case "${!#}" in
            refs/heads/main) echo "$PUBLIC_MAIN_SHA refs/heads/main" ;;
            refs/pull/4/head) echo "$PUBLIC_BASE_SHA refs/pull/4/head" ;;
        esac ;;
        *) echo 'unexpected transport' > attempted; return 96 ;;
    esac
}
python() {
    case "$*" in
        *pytest*) [[ "$SCENARIO" != tests-fail ]] ;;
        *audit_public.py*) if [[ "$SCENARIO" == gate-fail ]]; then return 1; fi
            printf '%s\n' "$REPORT" ;;
        *) "$VERIFIER" "$@" ;;
    esac
}
'''
    work = tmp_path / 'work'
    work.mkdir()
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    env = dict(os.environ, APPROVED_SHA='a' * 40, PUBLIC_MAIN_SHA='b' * 40,
               PUBLIC_BASE_SHA='c' * 40, BACKUP_BUNDLE='synthetic', EXPECTED_PRIVATE_COUNT='1',
               REVIEW_DIR=str(private), VERIFIER=sys.executable)
    env.pop('BASH_ENV', None)
    for name, data in [('private-values.txt', 'fixture\n'), ('reference.sha256', '0' * 64)]:
        path = private / name
        path.write_text(data)
        path.chmod(0o600)
    report = dict(scope='publication gate', result='pass', candidate_ref='refs/heads/feature/contact-privacy-admin',
                  candidate_commit=env['APPROVED_SHA'], public_base=env['PUBLIC_BASE_SHA'],
                  private_values_checked=1, archive_review={'result': 'pass'})
    for scenario in ('tests-fail', 'insecure-file', 'gate-fail', 'empty-report', 'moved-ref', 'check-only'):
        (private / 'private-values.txt').chmod(0o644 if scenario == 'insecure-file' else 0o600)
        env.update(SCENARIO=scenario, REPORT='' if scenario == 'empty-report' else json.dumps(report),
                   PUBLICATION_ACTION='check' if scenario == 'check-only' else 'push')
        run = subprocess.run(['bash', '-c', mocks + script], cwd=work, env=env, capture_output=True)
        assert (run.returncode == 0) == (scenario == 'check-only'), scenario
        assert not (work / 'attempted').exists(), scenario


def test_publication_requires_approved_branch_and_public_ancestor(repository, publication_evidence):
    evidence = publication_evidence
    # An unrelated commit in the same repository is not an approved publishing boundary.
    tree = git(repository, 'rev-parse', 'HEAD^{tree}').decode().strip()
    unrelated = git(repository, 'commit-tree', tree, '-m', 'independent synthetic root').decode().strip()
    for invalid in ({'ref': 'main'}, {'expected_sha': evidence['public_base']},
                    {'expected_sha': evidence['expected_sha'][:8]}, {'public_base': unrelated}):
        with pytest.raises(ValueError):
            guard.audit_for_publication(repository, **{**evidence, **invalid})
    result = guard.audit_for_publication(repository, **evidence)
    assert result['result'] == 'pass' and result['archive_review']['result'] == 'pass'
    assert result['candidate_commit'] == evidence['expected_sha']
    assert result['candidate_ref'] == evidence['ref'] and result['public_base'] == evidence['public_base']
    assert result['commits_checked'] == 2 and result['private_values_checked'] == 1


def test_publication_rejects_incomplete_or_substituted_private_reference(repository, publication_evidence):
    evidence = publication_evidence
    for raw, count in ((b'', 0), (b'one\n\n', 1), (b'one\nONE\n', 2), (b'one\n', 2)):
        expected = {**evidence, 'private_reference': raw, 'private_values_sha256': hashlib.sha256(raw).hexdigest(),
                    'expected_private_count': count}
        with pytest.raises(ValueError):
            guard.audit_for_publication(repository, **expected)
    with pytest.raises(ValueError, match='checksum'):
        guard.audit_for_publication(repository, **{**evidence, 'private_reference': b'replaced-fixture\n'})
    hidden_ref = b'heads/main\n'
    with pytest.raises(ValueError, match='not safe for public reporting') as error:
        guard.audit_for_publication(repository, **{**evidence, 'private_reference': hidden_ref,
                                    'private_values_sha256': hashlib.sha256(hidden_ref).hexdigest()})
    assert hidden_ref.strip().decode() not in str(error.value)


def test_publication_ref_drift_blocks_stale_result(repository, publication_evidence, monkeypatch):
    read_tip = guard.branch_tip
    scan = guard.audit

    def scan_then_move_candidate(*args, **kwargs):
        result = scan(*args, **kwargs)
        monkeypatch.setattr(guard, 'branch_tip', lambda *_: publication_evidence['public_base'])
        return result

    monkeypatch.setattr(guard, 'audit', scan_then_move_candidate)
    assert read_tip(repository, publication_evidence['ref']) == publication_evidence['expected_sha']
    with pytest.raises(ValueError, match='changed during review'):
        guard.audit_for_publication(repository, **publication_evidence)


def test_publication_cli_cannot_skip_checks_or_publish_historical_match(repository, publication_evidence):
    evidence = publication_evidence
    path = repository / 'temporary.txt'
    path.write_bytes(evidence['private_reference'])
    commit(repository, 'synthetic historical match')
    path.unlink()
    commit(repository, 'clean tip')
    tip = git(repository, 'rev-parse', 'HEAD').decode().strip()
    tools = repository / 'tools'
    tools.mkdir()
    script = tools / 'audit_public.py'
    script.write_bytes((ROOT / 'tools' / script.name).read_bytes())
    checks = {'--ref': evidence['ref'], '--expected-sha': tip, '--public-base': evidence['public_base'],
              '--expected-private-count': '1', '--private-values-sha256': evidence['private_values_sha256'],
              '--private-values-stdin': None}
    for missing in [*checks, None]:
        cmd = [sys.executable, '-B', str(script), '--publication']
        for flag, value in checks.items():
            if flag != missing:
                cmd.append(flag)
                if value is not None:
                    cmd.append(value)
        run = subprocess.run(cmd, input=evidence['private_reference'], capture_output=True)
        assert run.returncode == (2 if missing else 1)
        assert evidence['private_reference'].strip() not in run.stdout + run.stderr
        if missing is None:
            report = json.loads(run.stdout)
            assert report['result'] == 'fail' and report['findings'][0]['issues'] == ['known-private-value']
            duplicate = subprocess.run([*cmd, '--ref', 'refs/heads/other'], input=evidence['private_reference'],
                                       capture_output=True)
            assert duplicate.returncode == 2 and b'more than once' in duplicate.stderr
    marker = evidence['private_reference'].strip()
    for invalid in ([marker.decode()], ['--expected-private-count', marker.decode()]):
        bad = subprocess.run([sys.executable, '-B', str(script), *invalid], capture_output=True)
        assert bad.returncode == 2 and marker not in bad.stdout + bad.stderr


def test_ref_rejects_ambiguous_inputs_and_incomplete_history(repository):
    (repository / 'public.txt').write_text('safe')
    commit(repository, 'base')
    for ref in ('main~1', '--all', 'main..main', 'refs/tags/example', 'missing'):
        with pytest.raises(ValueError):
            guard.audit(repository, ref=ref)
    with pytest.raises(ValueError):
        guard.audit(repository, ref='main', staged=True)
    with pytest.raises(ValueError, match='competing history scopes'):
        guard.audit(repository, ref='main', history=True)
    cli = subprocess.run([sys.executable, '-B', str(ROOT/'tools'/'audit_public.py'), '--ref', 'main', '--history'],
                         capture_output=True, text=True)
    assert cli.returncode == 2 and '--ref' in cli.stderr and '--history' in cli.stderr
    shallow = repository / '.git' / 'shallow'
    shallow.write_bytes(git(repository, 'rev-parse', 'HEAD'))
    with pytest.raises(ValueError, match='complete repo'):
        guard.audit(repository, ref='main')


def test_known_private_input_scans_history_and_redacts_path(repository):
    marker = 'private-host.fixture.invalid'
    source = repository / marker
    source.write_text('endpoint')
    commit(repository, 'synthetic file')
    source.unlink()
    (repository / 'public.txt').write_text('safe')
    commit(repository, 'remove synthetic file')
    tools = repository / 'tools'
    tools.mkdir()
    script = tools / 'audit_public.py'
    script.write_bytes((ROOT / 'tools' / script.name).read_bytes())
    command = [sys.executable, '-B', str(script), '--ref', 'main', '--private-values-stdin']
    result = subprocess.run(command, input=marker.upper() + '\n', capture_output=True, text=True)
    assert result.returncode == 1 and marker not in (result.stdout + result.stderr).lower()
    report = json.loads(result.stdout)
    assert report['private_values_checked'] == 1 and report['commits_checked'] == 2
    assert report['findings'][0]['file'] == '(redacted path)'
    assert report['findings'][0]['issues'] == ['known-private-value']
    empty = subprocess.run(command, input='', capture_output=True, text=True)
    assert empty.returncode != 0


def test_phone_review_and_known_private_literals_also_cover_fixtures(repository):
    number = b'010' + b'-0000-9999'
    assert 'possible-personal-phone' in guard.inspect_blob('profile.txt', number)
    path = repository / 'tests' / 'synthetic.txt'
    path.parent.mkdir()
    path.write_bytes(number)
    commit(repository, 'synthetic phone')
    result = guard.audit(repository, ref='main', private_values=(number,))
    assert result['phone_fixture_files_to_review'] == ['tests/synthetic.txt']
    assert result['findings'][0]['issues'] == ['known-private-value']
    assert number.decode() not in json.dumps(result)
    flagged_path = repository / ('page_' + 'f' * 32)
    flagged_path.write_text('safe content')
    commit(repository, 'synthetic filename')
    result = guard.audit(repository, ref='main')
    assert result['findings'][0]['file'] == '(redacted path)'
    assert result['findings'][0]['issues'] == ['internal-project-page']
    assert flagged_path.name not in json.dumps(result)


@pytest.mark.parametrize('name,data,issue', [
    ('export.tar.gz', b'fixture', 'runtime-or-binary-file'),
    ('archive.bundle', b'fixture', 'runtime-or-binary-file'),
    ('notes.md', b'/recover#' + b'x' * 43, 'literal-recovery-link'),
    ('notes.md', b'__Host-collection=' + b'x' * 43, 'literal-session-token'),
])
def test_private_export_and_token_literals_are_rejected(name, data, issue):
    assert issue in guard.inspect_blob(name, data)


def png_chunk(kind, payload):
    contents = kind + payload
    return struct.pack('>I', len(payload)) + contents + struct.pack('>I', zlib.crc32(contents))


def test_png_rejects_hidden_metadata_trailing_or_corrupt_bytes():
    header = png_chunk(b'IHDR', struct.pack('>2I5B', 1, 1, 8, 6, 0, 0, 0))
    pixels = png_chunk(b'IDAT', zlib.compress(bytes(5))) + png_chunk(b'IEND', b'')
    start = guard.PNG_SIGNATURE + header
    assert guard.inspect_blob('art.png', start + pixels) == []
    for kind in (b'eXIf', b'tEXt', b'iTXt', b'zTXt', b'iCCP', b'prIv'):
        assert guard.inspect_blob('renamed.bin', start + png_chunk(kind, b'fixture') + pixels) == ['unreviewed-png-metadata']
    for malformed in (start + pixels + b'extra', start + pixels[:-1], start + pixels[:-2] + b'xx', b'not an image'):
        assert guard.inspect_blob('art.png', malformed) == ['invalid-png']


@pytest.mark.parametrize('failure,code', [
    ('private_source', 'RELEASE_SOURCE_REJECTED'),
    ('unsupported_entry', 'RELEASE_ENTRY'),
    ('unknown_ref', 'RELEASE_GIT'),
    ('output_path', 'RELEASE_OUTPUT_EXISTS'),
    ('invalid_argument', 'RELEASE_ARGUMENTS'),
])
def test_release_cli_redacts_errors(repository, tmp_path, failure, code):
    package_fixture(repository)
    marker = 'synthetic-010-' + '0000-9999'
    output = tmp_path / marker
    options = ['--version', 'v1.4.0', '--out', str(output)]
    if failure == 'private_source':
        (repository / (marker + '.env')).write_text('synthetic fixture')
        commit(repository, 'synthetic rejected file')
    elif failure == 'unsupported_entry':
        # Construct a Git link entry without requiring OS symlink privileges.
        oid = git(repository, 'rev-parse', 'HEAD:app/app.py').decode().strip()
        git(repository, 'update-index', '--add', '--cacheinfo', '120000', oid, marker)
        git(repository, 'commit', '-m', 'synthetic unsupported entry')
    elif failure == 'unknown_ref':
        options += ['--ref', marker]
    elif failure == 'output_path':
        output.write_text('occupied')
    elif failure == 'invalid_argument':
        options.append('--' + marker)

    scripts = repository / 'tools'
    scripts.mkdir()
    for name in ('audit_public.py', 'build_release.py'):
        (scripts / name).write_bytes((ROOT / 'tools' / name).read_bytes())
    proc = subprocess.run([sys.executable, '-B', str(scripts / 'build_release.py'), *options],
                          capture_output=True, text=True)
    assert proc.returncode == (2 if failure == 'invalid_argument' else 1)
    assert proc.stdout == ''
    error = proc.stderr.strip()
    if failure in ('private_source', 'unsupported_entry'):
        prefix = f'release error: {code} (tree entry '
        assert error.startswith(prefix) and error.endswith(')')
        assert error[len(prefix):-1].isdecimal()
    else:
        assert error == 'release error: ' + code
    assert marker not in error and str(tmp_path) not in error
    assert not output.exists() or output.is_file()
