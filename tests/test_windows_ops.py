"""Portable operation tests. Windows services/Task Scheduler are NOT exercised here."""
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('windows_ops_v14', PACKAGE/'scripts/ops.py')
ops=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def installed(tmp_path):
    root=tmp_path/'Maison Cafe 한글'
    for name in ('config','data','backups','logs'):
        (root/name).mkdir(parents=True)
    shutil.copytree(PACKAGE/'app',root/'app',ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
    event=ops.read_json(PACKAGE/'config/event.example.json')
    now=datetime.now(timezone.utc)
    event.update(draft=False, starts_at=(now-timedelta(days=1)).isoformat(),
                 ends_at=(now+timedelta(days=25)).isoformat(),reward_until=(now+timedelta(days=30)).isoformat())
    ops.write_json(root/'config/event.json',event)
    cfg=ops.read_json(PACKAGE/'config/operations.example.json')
    cfg.update(PublicUrl='https://stamps.example.invalid',MinimumFreeMB=16)
    ops.write_json(root/'config/operations.json',cfg)
    ops.validate(root,initialize=True)
    return root


def visitor(root,cookie=None):
    app=ops.load_source(root/'app').create_app(root/'config/event.json',root/'data/collection.sqlite',
                                              'https://stamps.example.invalid',testing=True)
    client=app.test_client()
    if cookie: client.set_cookie(app.cookie_name,cookie,domain='stamps.example.invalid')
    return app,client


def post(client,path,body,csrf=''):
    return client.post(path,json=body,base_url='https://stamps.example.invalid',headers={
        'Origin':'https://stamps.example.invalid','X-Stamp-Client':'1','X-CSRF-Token':csrf})


def state(client):
    return client.get('/api/state',base_url='https://stamps.example.invalid').json


def populated(root):
    app,client=visitor(root)
    assert post(client,'/api/session',{}).status_code==200
    csrf=state(client)['csrf']
    with ops.readonly(root/'data/collection.sqlite') as db:
        keys=[r[0] for r in db.execute('SELECT qr_key FROM products ORDER BY id')]
    assert post(client,'/api/collect',{'qr':keys[0]},csrf).json['added']
    token=post(client,'/api/recovery-link',{'confirm_rotation':True},csrf).json['url'].split('#')[1]
    return app,client,keys,token


def test_config_space_unicode_existing_db_validation_is_readonly(installed):
    populated(installed)
    paths=[installed/'data/collection.sqlite',installed/'config/event.json']
    before=[sha(p) for p in paths]
    for _ in range(3):
        assert ops.validate(installed,initialize=True)['counts']['customers']==1
    assert before==[sha(p) for p in paths]


def test_draft_and_invalid_url_never_initialize(installed):
    db=installed/'data/collection.sqlite'; db.unlink()
    event=ops.read_json(installed/'config/event.json');event['draft']=True
    ops.write_json(installed/'config/event.json',event)
    with pytest.raises(ops.OpsError):ops.validate(installed,initialize=True)
    assert not db.exists()
    event['draft']=False;ops.write_json(installed/'config/event.json',event)
    cfg=ops.settings(installed,False);cfg['PublicUrl']='http://localhost:8765'
    ops.write_json(installed/'config/operations.json',cfg)
    with pytest.raises(ops.OpsError):ops.validate(installed,initialize=True)
    assert not db.exists()


@pytest.mark.parametrize('url',['https://user:secret@example.com','https://example.com/path','https://example.com?x=1',
                                'https://example.com:443','https://LOCALHOST','https://example.com#token'])
def test_canonical_domain_required(installed,url):
    cfg=ops.settings(installed,False);cfg['PublicUrl']=url
    ops.write_json(installed/'config/operations.json',cfg)
    with pytest.raises(ops.OpsError):ops.settings(installed)


def test_other_event_refused_without_overwrite(installed):
    db=installed/'data/collection.sqlite';before=sha(db)
    event=ops.read_json(installed/'config/event.json');event['id']='different-event'
    ops.write_json(installed/'config/event.json',event)
    with pytest.raises(ops.OpsError):ops.validate(installed,initialize=True)
    assert sha(db)==before


def test_safe_snapshot_live_data_and_existing_target_refused(installed,tmp_path):
    populated(installed)
    db=installed/'data/collection.sqlite';out=tmp_path/'backup with spaces.sqlite'
    _,event=ops.event_for(installed)
    with sqlite3.connect(db) as connection:
        connection.execute('BEGIN IMMEDIATE')
        ops.snapshot(db,out,16,event)
    assert ops.inspect_database(out,event)=={'customers':1,'stamps':1,'reward_audit':0}
    before=sha(out)
    with pytest.raises(ops.OpsError):ops.snapshot(db,out,16,event)
    assert sha(out)==before
    assert not list(tmp_path.glob('*.partial*'))


def test_retention_exact_boundaries_and_iso_year(tmp_path):
    base=tmp_path/'backups'
    (base/'daily').mkdir(parents=True);(base/'weekly').mkdir()
    for day in ['20201220','20201221','20210103']:(base/'daily'/('maison-'+day+'T030000-a.sqlite')).touch()
    for year,week in [(2020,49),(2020,50),(2020,51),(2020,52),(2020,53)]:
        (base/'weekly'/f'maison-week-{year}-{week:02}-x.sqlite').touch()
    (base/'daily/customer-manual.sqlite').touch()
    ops.prune(base,datetime(2021,1,3,tzinfo=timezone.utc))
    assert sorted(p.name[7:15] for p in (base/'daily').glob('maison-*'))==['20201221','20210103']
    assert sorted(p.name[17:19] for p in (base/'weekly').glob('*'))==['50','51','52','53']
    assert (base/'daily/customer-manual.sqlite').exists()


def test_backup_daily_weekly_external_and_receipt(installed,tmp_path):
    populated(installed)
    cfg=ops.settings(installed,False);cfg['ExternalBackupPaths']=[str(tmp_path/'external disk')]
    ops.write_json(installed/'config/operations.json',cfg)
    first=ops.backup(installed,datetime(2026,11,2,3,tzinfo=timezone.utc))
    second=ops.backup(installed,datetime(2026,11,3,3,tzinfo=timezone.utc))
    assert first['status']==second['status']=='ok'
    assert len(list((installed/'backups/weekly').glob('*.sqlite')))==1
    assert len(list((tmp_path/'external disk/MaisonCafe/daily').glob('*.sqlite')))==2
    assert ops.read_json(installed/'backups/latest.json')['counts']['stamps']==1


def test_external_failure_keeps_local_and_no_prune(installed,tmp_path):
    cfg=ops.settings(installed,False);cfg['ExternalBackupPaths']=[str(tmp_path/'offline')]
    ops.write_json(installed/'config/operations.json',cfg)
    old=installed/'backups/daily/maison-20000101T030000-old.sqlite';old.parent.mkdir();old.touch()
    with pytest.raises(ops.OpsError):ops.backup(installed,blocked_external=cfg['ExternalBackupPaths'])
    receipt=ops.read_json(installed/'backups/latest.json')
    assert receipt['status']=='partial' and Path(receipt['daily']).exists() and old.exists()
    assert not (tmp_path/'offline').exists()


def test_disk_full_does_not_delete_old_backups(installed,monkeypatch):
    old=installed/'backups/daily/maison-20000101T030000-old.sqlite';old.parent.mkdir();old.write_bytes(b'keep')
    monkeypatch.setattr(ops.shutil,'disk_usage',lambda p:SimpleNamespace(free=1))
    with pytest.raises(ops.OpsError,match='Insufficient'):ops.backup(installed)
    assert old.read_bytes()==b'keep'
    assert not list((installed/'backups').rglob('*.partial*'))


def test_backup_lock_prevents_overlap(installed):
    with ops.file_lock(installed/'data/backup.lock'):
        with pytest.raises(ops.OpsError):ops.backup(installed)


def test_restore_stage_keeps_live_db_cookie_recovery_and_qr(installed):
    app,client,keys,token=populated(installed)
    cookie=client.get_cookie(app.cookie_name,domain='stamps.example.invalid').value
    receipt=ops.backup(installed)
    before=sha(installed/'data/collection.sqlite')
    candidate=ops.stage_restore(installed,receipt['daily'])
    assert candidate['counts']['stamps']==1 and sha(installed/'data/collection.sqlite')==before
    _,again=visitor(installed,cookie)
    assert state(again)['customer']['count']==1
    assert post(again,'/api/collect',{'qr':keys[0]},state(again)['csrf']).json['added'] is False
    _,fresh=visitor(installed)
    assert post(fresh,'/api/recover',{'token':token}).status_code==200
    assert state(fresh)['customer']['count']==1


def test_restore_requires_stopped_server_and_untampered_candidate(installed):
    candidate=ops.stage_restore(installed,ops.backup(installed)['daily'])['candidate']
    with pytest.raises(ops.OpsError):ops.activate_restore(installed,candidate,True)
    (installed/'config/maintenance.flag').touch()
    Path(candidate).write_bytes(b'broken')
    before=sha(installed/'data/collection.sqlite')
    with pytest.raises(ops.OpsError):ops.activate_restore(installed,candidate,True)
    assert sha(installed/'data/collection.sqlite')==before


def test_restore_activation_preserves_original_and_reward_idempotence(installed):
    app,client,keys,token=populated(installed)
    csrf=state(client)['csrf']
    for key in keys[1:]:assert post(client,'/api/collect',{'qr':key},csrf).status_code==200
    for _ in range(2):assert post(client,'/api/redeem',{'confirm_received':True},csrf).status_code==200
    candidate=ops.stage_restore(installed,ops.backup(installed)['daily'])['candidate']
    before=sha(installed/'data/collection.sqlite')
    (installed/'config/maintenance.flag').touch()
    with ops.file_lock(installed/'data/server.lock'):
        with pytest.raises(ops.OpsError):ops.activate_restore(installed,candidate,True)
    result=ops.activate_restore(installed,candidate,True)
    assert sha(Path(result['retired'])/'collection.sqlite')==before
    assert result['counts']=={'customers':1,'stamps':10,'reward_audit':1}
    _,fresh=visitor(installed)
    assert post(fresh,'/api/recover',{'token':token}).status_code==200
    assert post(fresh,'/api/redeem',{'confirm_received':True},state(fresh)['csrf']).json['already_redeemed'] is True


def test_import_only_to_missing_database(installed,tmp_path):
    populated(installed)
    source=ops.snapshot(installed/'data/collection.sqlite',tmp_path/'migrate.sqlite',16)
    with pytest.raises(ops.OpsError):ops.import_database(installed,source)
    (installed/'data/collection.sqlite').unlink()
    assert ops.import_database(installed,source)['counts']['stamps']==1
    assert source.exists()


def test_package_checksums_and_traversal(tmp_path):
    package=tmp_path/'package';package.mkdir();(package/'ok').write_bytes(b'ok')
    manifest={'format':'maison-windows-v1','version':'1.4','sha256':{'ok':sha(package/'ok')}}
    ops.write_json(package/'PACKAGE_MANIFEST.json',manifest)
    assert ops.check_package(package)['package']=='ok'
    (package/'ok').write_bytes(b'changed')
    with pytest.raises(ops.OpsError):ops.check_package(package)
    manifest['sha256']={'../outside':'x'};ops.write_json(package/'PACKAGE_MANIFEST.json',manifest)
    with pytest.raises(ops.OpsError):ops.check_package(package)


def test_candidate_update_selftest_rejects_bad_code_preserves_live(installed):
    populated(installed)
    before=sha(installed/'data/collection.sqlite')
    candidate=installed/'app.next';candidate.mkdir();(candidate/'app.py').write_text('raise RuntimeError("bad candidate")')
    with pytest.raises(RuntimeError):ops.selftest(installed,candidate)
    assert sha(installed/'data/collection.sqlite')==before


def test_windows_scripts_declare_production_safety_contracts():
    def script(name):return (PACKAGE/'scripts'/name).read_text(encoding='utf-8-sig')
    task=script('Register-Autostart.ps1')
    assert "-UserId 'SYSTEM' -LogonType ServiceAccount" in task
    assert '-AtStartup' in task and '-MultipleInstances IgnoreNew' in task
    assert '-RestartCount 999' in task and '-ExecutionTimeLimit ([TimeSpan]::Zero)' in task
    assert 'Global\\' in script('Run-Server.ps1')
    assert '-AsSecureString' in script('Setup-Tunnel.ps1')
    assert 'run --token-file ' in script('Setup-Tunnel.ps1') and 'run --token ' not in script('Setup-Tunnel.ps1')
    assert 'sc.exe failure Cloudflared' in script('Setup-Tunnel.ps1')


@pytest.mark.skipif(__import__('os').environ.get('MAISON_HTTP_TESTS')!='1',reason='Explicit loopback HTTP test opt-in')
def test_real_waitress_selftest_uses_clone_and_closes_listener(installed):
    populated(installed)
    before=sha(installed/'data/collection.sqlite')
    assert ops.selftest(installed)['selftest']=='ok'
    assert before==sha(installed/'data/collection.sqlite')
    assert not list((installed/'data/validation').glob('check-*'))


@pytest.mark.skipif(__import__('os').environ.get('MAISON_HTTP_TESTS')!='1',reason='Explicit loopback HTTP test opt-in')
def test_real_server_restart_duplicate_process_and_persisted_cookie(installed):
    import socket
    import subprocess
    import time
    from urllib.request import Request,urlopen
    app,client,keys,token=populated(installed)
    cookie=client.get_cookie(app.cookie_name,domain='stamps.example.invalid').value
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    cfg=ops.settings(installed,False);cfg['Port']=port;ops.write_json(installed/'config/operations.json',cfg)
    command=[sys.executable,str(PACKAGE/'scripts/ops.py'),'--root',str(installed),'serve']
    def start():
        child=subprocess.Popen(command,cwd=installed/'logs',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if child.poll() is not None:pytest.fail('Server exited: '+str(child.communicate()))
            try:
                if ops.request_health(cfg):return child
            except OSError:pass
            time.sleep(.1)
        child.kill();child.communicate();pytest.fail('Server startup timed out')
    def get_state():
        req=Request('http://127.0.0.1:'+str(port)+'/api/state',headers={
            'Host':'stamps.example.invalid','X-Forwarded-Proto':'https','Cookie':app.cookie_name+'='+cookie})
        with urlopen(req,timeout=3) as response:return json.load(response)
    first=start()
    try:
        assert get_state()['customer']['count']==1
        duplicate=subprocess.run(command,capture_output=True,text=True,timeout=10)
        assert duplicate.returncode==1 and 'lock' in duplicate.stderr
        (installed/'config/stop.flag').touch()
        assert first.wait(timeout=30)==0, first.stderr.read().decode('utf-8',errors='replace')
        (installed/'config/stop.flag').unlink()
        second=start()
        try:assert get_state()['customer']['count']==1
        finally:
            (installed/'config/maintenance.flag').touch()
            assert second.wait(timeout=30)==0, second.stderr.read().decode('utf-8',errors='replace')
    finally:
        if first.poll() is None:first.kill();first.wait()
        first.communicate()
    assert not (installed/'config/server.state.json').exists()


def test_corrupt_restore_never_changes_live_database(installed,tmp_path):
    source=tmp_path/'corrupt.sqlite';source.write_bytes(b'not a database')
    before=sha(installed/'data/collection.sqlite')
    with pytest.raises(sqlite3.DatabaseError):ops.stage_restore(installed,source)
    assert sha(installed/'data/collection.sqlite')==before
    assert not list((installed/'data/recovery-check').glob('*'))


def test_restore_publish_failure_rolls_back_original(installed,monkeypatch):
    populated(installed)
    candidate=ops.stage_restore(installed,ops.backup(installed)['daily'])['candidate']
    before=sha(installed/'data/collection.sqlite')
    (installed/'config/maintenance.flag').touch()
    original=Path.rename
    def fail_publish(self,target):
        if self.name.startswith('activate-'):raise OSError('simulated publish failure')
        return original(self,target)
    monkeypatch.setattr(Path,'rename',fail_publish)
    with pytest.raises(OSError):ops.activate_restore(installed,candidate,True)
    assert sha(installed/'data/collection.sqlite')==before
    assert ops.inspect_database(installed/'data/collection.sqlite')['stamps']==1
    assert list((installed/'backups/pre-restore').glob('*.sqlite'))
