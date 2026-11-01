"""Field-tool safety/unit checks; full HTTP run is explicit, not multiplied per CI matrix."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('field_http', ROOT/'tools/field_http.py')
field = importlib.util.module_from_spec(spec)
spec.loader.exec_module(field)


def test_prepare_creates_synthetic_settings_only_and_refuses_reuse(tmp_path):
    dest = tmp_path/'field space 한글'
    source_before = (ROOT/'config/event.example.json').read_bytes()
    field.prepare(dest, 'https://field.example.invalid')
    event = json.loads((dest/'config/event.json').read_text())
    cfg = json.loads((dest/'config/operations.json').read_text())
    assert event['id'].startswith('field-') and not event['draft']
    assert len(event['products'])==10 and all(p['name'].startswith('TEST') for p in event['products'])
    assert cfg['ExternalBackupPaths']==[] and cfg['Port']!=cfg['MetricsPort']
    assert not (dest/'data').exists() and not (dest/'app').exists()
    before = (dest/'config/event.json').read_bytes()
    with pytest.raises(field.CheckFailed): field.prepare(dest, 'https://field.example.invalid')
    assert (dest/'config/event.json').read_bytes()==before
    assert (ROOT/'config/event.example.json').read_bytes()==source_before


def test_existing_root_and_symlink_are_never_used(tmp_path):
    dest = tmp_path/'existing'; dest.mkdir()
    (dest/'sentinel').write_text('keep')
    with pytest.raises(field.CheckFailed): field.new_output(dest)
    assert (dest/'sentinel').read_text()=='keep'
    link = tmp_path/'linked'
    try: link.symlink_to(dest, target_is_directory=True)
    except OSError: pytest.skip('Symlink creation not permitted on this host')
    with pytest.raises(field.CheckFailed): field.new_output(link/'new')
    assert not (dest/'new').exists()


def test_runtime_guard_precedes_any_output_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(field,'inspect_runtime',lambda:dict(python='0',sqlite='0',sqlite_source_id='bad'))
    with pytest.raises(field.CheckFailed): field.run(tmp_path/'not-created')
    assert not (tmp_path/'not-created').exists()


def test_statistics_keep_errors_and_groups_without_payloads():
    rows=[dict(group='normal',route='/api/collect',status=200,ms=1),
          dict(group='normal',route='/api/collect',status=None,ms=20),
          dict(group='injected_admission',route='/api/collect',status=503,ms=15000)]
    result=field.summarize(rows)
    assert result['normal /api/collect']['n']==2
    assert result['normal /api/collect']['statuses']=={'200':1,'None':1}
    assert result['injected_admission /api/collect']['p99_ms']==15000


@pytest.mark.parametrize('origin', [
    'http://field.example.invalid', 'https://user:password@field.example.invalid',
    'https://field.example.invalid/path', 'https://field.example.invalid:443',
    'https://FIELD.example.invalid',
])
def test_invalid_settings_never_publish_output(tmp_path, origin):
    target = tmp_path/'not-created'
    with pytest.raises((field.CheckFailed, field.ops.OpsError)):
        field.prepare(target, origin)
    assert not target.exists()


class ShutdownSocket:
    """Model Windows EOF/WSAESHUTDOWN without cross-thread socket operations."""
    def __init__(self, result):
        import threading
        self.owner = threading.get_ident()
        self.result, self.calls = result, []
    def record(self, name):
        import threading
        assert threading.get_ident() == self.owner
        self.calls.append(name)
    def setblocking(self, value):
        self.record('nonblocking'); assert value is False
    def shutdown(self, how):
        self.record('shutdown'); assert how == field.socket.SHUT_RDWR
    def recv(self, size, flags=0):
        self.record('peek' if flags else 'receive')
        if flags:
            assert flags == field.socket.MSG_PEEK
            raise BlockingIOError()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize('result', [b'', OSError(10058, 'synthetic receive shutdown')])
def test_unread_reply_loss_has_confirmed_eof_or_shutdown(result):
    sock = ShutdownSocket(result)
    assert field.abort_unread_reply(sock) in ('eof_after_shutdown', 'connection_ended_after_shutdown')
    assert sock.calls == ['nonblocking','peek','shutdown','receive']


@pytest.mark.parametrize('result', [TimeoutError(), BlockingIOError(), OSError(5, 'synthetic I/O error'), b'HTTP'])
def test_timeout_pending_io_and_received_bytes_do_not_count_as_loss(result):
    with pytest.raises((field.CheckFailed, OSError)):
        field.abort_unread_reply(ShutdownSocket(result))


@pytest.mark.parametrize('result,success', [(OSError(10058,'synthetic receive shutdown'),True), (TimeoutError(),False), (b'HTTP',False)])
def test_reply_loss_one_owner_cleanup_and_no_client_executor(tmp_path, monkeypatch,result,success):
    import sqlite3
    import threading
    from types import SimpleNamespace
    root = tmp_path/'trial'; (root/'data').mkdir(parents=True)
    with sqlite3.connect(root/'data/collection.sqlite') as db:
        db.execute('CREATE TABLE reward_audit (customer_id TEXT PRIMARY KEY)')
        db.execute("INSERT INTO reward_audit VALUES ('synthetic')")
    committed, deliver, replied = threading.Event(), threading.Event(), threading.Event()
    committed.set()
    gate = SimpleNamespace(armed=False, committed=committed, deliver=deliver, replied=replied, upstream_200=True)
    sock = ShutdownSocket(result)
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = sock
        def request(self, *args): sock.record('request')
        def getresponse(self): raise AssertionError('No blocking reader thread allowed')
        def close(self): sock.record('close')
    monkeypatch.setattr(field.http.client, 'HTTPConnection', Connection)
    def no_executor(*a, **k): raise AssertionError('No client receiver executor allowed')
    monkeypatch.setattr(field, 'ThreadPoolExecutor', no_executor)
    client = SimpleNamespace(server=SimpleNamespace(gate=gate,port=1), observations=[],headers=lambda:{},
        read=lambda:{'customer':{'status':'redeemed'}},request=lambda *a:{'already_redeemed':True})
    diagnostic = {}
    if success:
        field.response_loss(client, root, diagnostic)
        assert diagnostic['phase']=='complete' and diagnostic['single_reward_confirmed']
    else:
        with pytest.raises((field.CheckFailed, TimeoutError)):
            field.response_loss(client, root, diagnostic)
        assert diagnostic['phase']=='abort_unread_reply' and not diagnostic.get('single_reward_confirmed')
    assert sock.calls[-1]=='close' and deliver.is_set()
    assert diagnostic['commit_confirmed']
    assert diagnostic['connection_closed'] and diagnostic['gate_released'] and not diagnostic['client_receiver_thread']


@pytest.mark.skipif(__import__('os').environ.get('MAISON_HTTP_TESTS')!='1',reason='Explicit loopback HTTP test opt-in')
def test_reply_loss_real_waitress_commit_requery_and_thread_cleanup(tmp_path):
    from contextlib import closing
    root = field.prepare(tmp_path/'field actual HTTP')
    field.copy_public_app(root)
    field.ops.validate(root,initialize=True)
    server = field.Server(root); server.start()
    rows, diagnostic = [], {}
    try:
        client = field.Client(server,rows).start()
        with closing(field.ops.readonly(root/'data/collection.sqlite')) as db:
            keys = [r[0] for r in db.execute('SELECT qr_key FROM products ORDER BY id')]
        for key in keys: client.collect(key)
        field.response_loss(client,root,diagnostic)
        assert diagnostic['phase']=='complete'
        assert diagnostic['loss_evidence'] in ('eof_after_shutdown','connection_ended_after_shutdown')
        assert field.audit(root)['reward_audit']==1
        assert client.read()['customer']['status']=='redeemed'
    finally:
        server.stop()
    assert not server.thread.is_alive() and not server.sockets
    assert not server.server.task_dispatcher.threads


def test_already_received_data_is_not_injected_reply_loss():
    sock = ShutdownSocket(b'')
    sock.recv = lambda *a: b'H'
    with pytest.raises(field.CheckFailed,match='before_injection'):
        field.abort_unread_reply(sock)
    assert 'shutdown' not in sock.calls
