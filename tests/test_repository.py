"""Publication/history guards and exact-byte Git release packaging."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import audit_public as guard
import build_release


@pytest.mark.parametrize('name,data,expected', [
    ('data/collection.sqlite', b'anything', 'runtime-or-binary-file'),
    ('config/event.json', b'{}', 'private-config'),
    ('safe.txt', b'SQLite format 3\0payload', 'sqlite-content'),
    ('safe.py', ('ghp_'+'x'*36).encode(), 'github-token'),
    ('safe.txt', ('-----BEGIN '+'PRIVATE KEY-----').encode(), 'private-key'),
    ('safe.txt', ('https://example.invalid/q/'+'a'*32).encode(), 'literal-product-qr'),
])
def test_public_guard_rejects_runtime_files_and_credentials(name,data,expected):
    assert expected in guard.inspect_blob(name,data)


def test_only_synthetic_qr_decoder_fixture_is_allowed():
    value=('http://127.0.0.1:8765/q/'+'a'*32).encode()
    assert guard.inspect_blob('app/tests/fixtures/qr-matrices.json',value)==[]
    assert 'literal-product-qr' in guard.inspect_blob('docs/qr.md',value)
    assert 'non-example-event' in guard.inspect_blob('app/event.json',b'{"draft": false}')


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout


@pytest.fixture
def repository(tmp_path):
    root=tmp_path/'repo with spaces';root.mkdir()
    git(root,'init','-b','main')
    git(root,'config','user.name','Fixture')
    git(root,'config','user.email','fixture@example.invalid')
    return root


def commit(root,message):
    git(root,'add','--all');git(root,'commit','-m',message)


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
