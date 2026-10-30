"""Isolated field-tool preparation and real loopback Waitress checks.

No database/origin target option for run: always creates synthetic data in a new
output directory. Never imports operational config or copies runtime directories.
"""
import argparse
import copy
import hashlib
import http.client
import importlib.util
import json
import logging
import math
import os
import platform
import socket
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'tools'))
from audit_public import inspect_blob
from check_sqlite_runtime import inspect_runtime

spec = importlib.util.spec_from_file_location('field_existing_ops', SOURCE / 'scripts/ops.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
EXPECTED_SOURCE = '2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc'
ORIGIN = 'https://field.example.invalid'


class CheckFailed(Exception):
    pass


def require(ok, label):
    if not ok:
        raise CheckFailed(label)  # Static labels only, no payload/SQL/error text.


def new_output(path):
    path = Path(path).absolute()
    # Existing folders, symlink destinations and their runtime records are refused.
    require(not path.exists() and not path.is_symlink(), 'output_must_be_new')
    require(not any(p.is_symlink() for p in path.parents), 'linked_parent_refused')
    path.mkdir(parents=True, exist_ok=False)
    return path


def prepare(path, origin=ORIGIN, port=8875, metrics=20242):
    """Settings only, based on PUBLIC example; no DB, certificate or server."""
    # Validate with existing production rules before publishing output.
    parsed = urlsplit(origin)
    require(parsed.scheme == 'https' and bool(parsed.hostname), 'https_origin_required')
    require(type(port) is int and type(metrics) is int and 1024 <= port <= 65535
            and 1024 <= metrics <= 65535 and port != metrics, 'distinct_ports_required')
    event = ops.read_json(SOURCE / 'config/event.example.json')
    require(not inspect_blob('config/event.example.json', (SOURCE / 'config/event.example.json').read_bytes()), 'public_example_required')
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event.update(id='field-' + uuid.uuid4().hex, draft=False, cafe_name='Maison de Gourmet · TEST',
                 title='TEST ONLY · Winter Collection', reward='TEST ONLY · no physical reward',
                 starts_at=(now-timedelta(minutes=5)).isoformat(),
                 ends_at=(now+timedelta(days=30)).isoformat(),
                 reward_until=(now+timedelta(days=31)).isoformat())
    for product in event['products']:
        product['name'] = 'TEST · ' + product['name']
    config = ops.read_json(SOURCE / 'config/operations.example.json')
    config.update(PublicUrl=origin, Port=port, MetricsPort=metrics, ExternalBackupPaths=[])
    with tempfile.TemporaryDirectory(prefix='maison-field-config-') as check:
        ops.write_json(Path(check)/'config/operations.json', config)
        ops.settings(check)
    folder = new_output(path)
    (folder / 'config').mkdir()
    ops.write_json(folder / 'config/event.json', event)
    ops.write_json(folder / 'config/operations.json', config)
    ops.settings(folder)  # Canonical host/HTTPS and actual port/settings checks.
    ops.write_json(folder / 'FIELD_TEST_ONLY.json', {'synthetic_only': True, 'database_created': False})
    return folder


def copy_public_app(root):
    manifest = SOURCE / 'PACKAGE_MANIFEST.json'
    if manifest.is_file():
        ops.check_package(SOURCE)
        metadata = ops.read_json(manifest)
        names = metadata['sha256']
        source = dict(commit=metadata['source_commit'], package_label=metadata.get('release_tag'), dirty=False)
    else:
        names = subprocess.check_output(['git', '-C', str(SOURCE), 'ls-files', '-z', '--', 'app']).decode().split('\0')
        source = dict(commit=subprocess.check_output(['git','-C',str(SOURCE),'rev-parse','HEAD']).decode().strip(),
                      package_label=None, dirty=bool(subprocess.check_output(['git','-C',str(SOURCE),'status','--porcelain'])))
    for name in names:
        if not name.startswith('app/') or name.startswith(('app/tests/', 'app/tools/')):
            continue
        original = SOURCE / name
        require(not original.is_symlink() and original.resolve().is_relative_to(SOURCE), 'source_link_refused')
        content = original.read_bytes()
        require(not inspect_blob(name, content), 'private_app_content_refused')
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    require((root / 'app/app.py').is_file(), 'application_missing')
    return source


class ReplyGate:
    """One test-only redeem response withheld AFTER app completion/commit."""
    def __init__(self, app):
        self.app = app
        self.armed = False
        self.committed = threading.Event()
        self.deliver = threading.Event()
        self.upstream_200 = False

    def __call__(self, environ, start_response):
        if not (self.armed and environ['PATH_INFO'] == '/api/redeem'):
            return self.app(environ, start_response)
        self.armed = False
        captured = []
        chunks = []
        def capture(status, headers, exc_info=None):
            captured.append((status, headers))
            return chunks.append
        body = self.app(environ, capture)
        try:
            chunks.extend(body)
        finally:
            if hasattr(body, 'close'):
                body.close()
        self.upstream_200 = bool(captured and captured[0][0].startswith('200 '))
        self.committed.set()
        self.deliver.wait(10)  # Fail-safe: never strand a Waitress worker.
        start_response(*captured[0])
        return chunks


class Server:
    def __init__(self, root):
        self.root = root
        self.port = None
        self.running = False

    def start(self):
        module = ops.load_source(self.root / 'app')
        original = module.create_app
        def capture(*args, **kwargs):
            self.app = original(*args, **kwargs)
            self.gate = ReplyGate(self.app)
            return self.gate
        module.create_app = capture  # Only this NEW private fixture module.
        try:
            self.server, self.sockets, _ = ops.create_http(self.root, port=0)
        finally:
            module.create_app = original
        self.port = int(self.server.effective_port)
        self.stop_signal = threading.Event()
        self.thread = threading.Thread(target=ops.run_http,
            args=(self.server, self.sockets, self.stop_signal.is_set), daemon=True)
        self.thread.start()
        self.running = True
        require(not self.app.testing and not self.app.debug, 'production_mode_required')
        return self

    def stop(self):
        if not self.running:
            return
        self.gate.deliver.set()
        self.stop_signal.set()
        self.thread.join(10)
        require(not self.thread.is_alive(), 'server_stop_failed')
        ops.close_http(self.server, self.sockets)
        self.running = False


class Client:
    def __init__(self, server, observations, cookie=None, group='normal'):
        self.server, self.observations = server, observations
        self.group = group
        self.cookie, self.csrf = cookie, ''

    def headers(self):
        result = {'Host': urlsplit(ORIGIN).netloc, 'Origin': ORIGIN,
                  'Content-Type': 'application/json', 'X-Stamp-Client':'1',
                  'X-CSRF-Token': self.csrf, 'X-Forwarded-Proto':'https'}
        if self.cookie:
            result['Cookie'] = self.cookie
        return result

    def request(self, path, body=None, expected=200, group=None):
        started = time.perf_counter()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=20)
        status = None
        try:
            conn.request('GET' if body is None else 'POST', path,
                         None if body is None else json.dumps(body), self.headers())
            reply = conn.getresponse()
            status = reply.status
            raw = reply.read()
            if reply.getheader('Set-Cookie'):
                jar = SimpleCookie(); jar.load(reply.getheader('Set-Cookie'))
                require('__Host-collection' in jar and jar['__Host-collection']['secure']
                        and jar['__Host-collection']['httponly'], 'secure_cookie_missing')
                self.cookie = '__Host-collection=' + jar['__Host-collection'].value
            require(status == expected, 'unexpected_http_status')
            return json.loads(raw) if reply.getheader('Content-Type','').startswith('application/json') else None
        finally:
            conn.close()
            # Route category only; QR path/headers/body/identifiers never recorded.
            route = '/q/' if path.startswith('/q/') else path
            self.observations.append(dict(group=group or self.group, route=route, status=status,
                                          ms=round((time.perf_counter()-started)*1000,3)))

    def read(self):
        state = self.request('/api/state')
        self.csrf = state['csrf'] or ''
        return state

    def start(self):
        self.request('/api/session', {})
        require(self.read()['customer']['count'] == 0, 'new_card_failed')
        return self

    def collect(self, key, expected_added=True):
        result = self.request('/api/collect', {'qr':key})
        require(result['added'] is expected_added, 'unexpected_collection_result')
        return result


def logical_state(root, path=None):
    """Private in-memory comparison, never serialized or logged."""
    with closing(ops.readonly(path or root / 'data/collection.sqlite')) as db:
        return {name: sorted(map(tuple,db.execute('SELECT * FROM '+name))) for name in
                ('metadata','products','customers','sessions','stamps','recovery','reward_audit')}


def audit(root):
    counts = ops.inspect_database(root / 'data/collection.sqlite')
    with closing(ops.readonly(root / 'data/collection.sqlite')) as db:
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'foreign_key_failure')
        require(db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM stamps GROUP BY customer_id,product_id HAVING COUNT(*)>1)').fetchone()[0] == 0, 'duplicate_stamp')
        require(db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM reward_audit GROUP BY customer_id HAVING COUNT(*)>1)').fetchone()[0] == 0, 'duplicate_reward')
        require(db.execute('SELECT COUNT(*) FROM customers c WHERE (c.redeemed_at IS NOT NULL) != EXISTS(SELECT 1 FROM reward_audit a WHERE a.customer_id=c.id) OR (c.redeemed_at IS NOT NULL AND (SELECT COUNT(*) FROM stamps s WHERE s.customer_id=c.id)!=10)').fetchone()[0] == 0, 'reward_mismatch')
    return dict(counts, integrity_ok=True, foreign_keys_ok=True, duplicates=0, reward_mismatches=0)


def response_loss(client, root):
    gate = client.server.gate
    gate.armed = True
    conn = http.client.HTTPConnection('127.0.0.1', client.server.port, timeout=20)
    outcome = {'status':None}
    started = time.perf_counter()
    def send():
        try:
            conn.request('POST','/api/redeem',json.dumps({'confirm_received':True}),client.headers())
            reply = conn.getresponse()
            outcome['status'] = reply.status
            reply.read()
            return False
        except (OSError, http.client.HTTPException):
            return True
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(send)
            try:
                require(gate.committed.wait(8) and gate.upstream_200, 'upstream_commit_not_confirmed')
                with closing(ops.readonly(root / 'data/collection.sqlite')) as db:
                    require(db.execute('SELECT COUNT(*) FROM reward_audit').fetchone()[0] == 1, 'commit_not_persisted')
                require(conn.sock is not None, 'client_socket_missing')
                conn.sock.shutdown(socket.SHUT_RDWR)
                conn.close()  # Drop downstream before allowing any upstream reply.
                require(pending.result(timeout=5), 'response_was_not_lost')
            finally:
                gate.deliver.set()
                conn.close()
    finally:
        client.observations.append(dict(group='injected_response_loss',route='/api/redeem',
            status=outcome['status'],ms=round((time.perf_counter()-started)*1000,3)))
    require(client.read()['customer']['status'] == 'redeemed', 'lost_reply_state_failed')
    require(client.request('/api/redeem', {'confirm_received':True})['already_redeemed'], 'redeem_not_idempotent')


def admission_failure(client, key):
    ready, release = threading.Event(), threading.Event()
    acquired = []
    def hold():
        ok = client.server.app.write_lock.acquire(timeout=1)
        acquired.append(ok); ready.set()
        if ok:
            try: release.wait(20)
            finally: client.server.app.write_lock.release()
    thread = threading.Thread(target=hold, daemon=True); thread.start()
    try:
        require(ready.wait(2) and acquired == [True], 'controlled_lock_not_acquired')
        before = client.read()['customer']['count']
        started = time.monotonic()
        response = client.request('/api/collect', {'qr':key}, expected=503, group='injected_admission')
        require(response['code'] == 'storage_unavailable' and time.monotonic()-started >= 14.9, 'admission_deadline_changed')
        require(client.read()['customer']['count'] == before, 'failed_request_changed_stamp')
    finally:
        release.set(); thread.join(3)
        require(not thread.is_alive(), 'controlled_lock_not_released')
    client.collect(key)


def summarize(rows):
    groups = {}
    for group, route in sorted({(r['group'],r['route']) for r in rows}):
        values = [r for r in rows if (r['group'],r['route']) == (group,route)]
        times = sorted(r['ms'] for r in values)
        groups[group+' '+route] = dict(n=len(values), p95_ms=times[math.ceil(len(times)*.95)-1],
            p99_ms=times[math.ceil(len(times)*.99)-1], max_ms=times[-1],
            statuses={str(code):sum(r['status']==code for r in values) for code in sorted({r['status'] for r in values},key=str)})
    return groups


def run(path, tooling_smoke=False):
    runtime = inspect_runtime()
    target = (os.name == 'nt' and platform.machine().upper() in ('AMD64','X86_64')
              and runtime['python']=='3.15.2' and runtime['sqlite']=='3.53.4'
              and runtime['sqlite_source_id']==EXPECTED_SOURCE and runtime['implementation']=='cpython'
              and runtime['bits']==64 and not runtime['free_threaded'] and runtime['gil_enabled'])
    require(target or (tooling_smoke and os.name != 'nt'), 'verified_windows_candidate_runtime_required')
    root = prepare(path)
    report = dict(runtime=runtime, runtime_target_match=target, tooling_smoke=tooling_smoke,
                  os=platform.system(), os_version=platform.version(), architecture=platform.machine(),
                  field_acceptance='not_performed', performance_criteria='operator_approval_required', scope='loopback HTTP; no TLS, Windows service or mobile acceptance',
                  cases=[], stages=[], tool_checks_passed=False)
    rows, server = [], None
    try:
        report['source'] = copy_public_app(root)
        for name in ('data','backups','logs'): (root/name).mkdir()
        ops.validate(root, initialize=True)
        ops.write_json(root/'FIELD_TEST_ONLY.json', {'synthetic_only':True,'database_created':True})
        report['server'] = dict(kind='Waitress', testing=False, bind='127.0.0.1', threads=32)
        server = Server(root); server.start()
        with closing(ops.readonly(root/'data/collection.sqlite')) as db:
            keys = [r[0] for r in db.execute('SELECT qr_key FROM products ORDER BY id')]
        original_products = logical_state(root)['products']
        visitor = Client(server, rows)
        visitor.request('/q/'+keys[0]); require(audit(root)['customers']==0, 'qr_get_created_customer')
        visitor.start()
        require(visitor.request('/api/redeem',{'confirm_received':True},expected=409,group='expected_rejection')['code']=='incomplete', 'early_redeem_allowed')
        for key in keys: visitor.collect(key)
        visitor.collect(keys[0], False)
        require(visitor.read()['customer']['status']=='complete', 'collection_incomplete')
        response_loss(visitor, root)
        report['cases'].append('normal_duplicate_and_committed_reward_reply_loss_passed')
        returning = Client(server, rows, visitor.cookie)
        require(returning.read()['customer']['status']=='redeemed', 'persisted_cookie_failed')
        recovery = visitor.request('/api/recovery-link', {'confirm_rotation':True})['url'].split('#')[1]
        restored = Client(server, rows)
        restored.request('/api/recover',{'token':recovery})
        require(restored.read()['customer']['status']=='redeemed', 'recovery_failed')
        restored.request('/api/recovery-link', {'confirm_rotation':True})
        obsolete = Client(server, rows)
        require(obsolete.request('/api/recover',{'token':recovery},expected=400,group='expected_rejection')['code']=='invalid_recovery', 'rotated_link_not_rejected')
        report['cases'].append('cookie_recovery_and_rotation_http_passed')
        for number in (5,10,30):
            group = 'concurrent_'+str(number)
            stage = dict(group=group, clients=number, planned_collects=number*5, complete=False)
            report['stages'].append(stage)
            clients = [Client(server, rows, group=group).start() for _ in range(number)]
            started = time.monotonic()
            try:
                for index in range(5):
                    with ThreadPoolExecutor(max_workers=number) as pool:
                        list(pool.map(lambda c:c.collect(keys[index]), clients))
                    if index<4: time.sleep(10)
                require(all(c.read()['customer']['count']==5 for c in clients), 'concurrent_client_mismatch')
                stage['complete'] = True
            finally:
                elapsed = time.monotonic()-started
                attempts = [r for r in rows if r['group']==group and r['route']=='/api/collect']
                stage.update(attempted_collects=len(attempts), successful_collects=sum(r['status']==200 for r in attempts),
                             elapsed_seconds=round(elapsed,3), completed_collects_per_second=round(len(attempts)/elapsed,3))
            report['cases'].append('paced_'+str(number)+'_clients_5_bursts_passed')
            print('FIELD_TOOL_STAGE paced_'+str(number)+'_passed', flush=True)
        probe = Client(server, rows).start()
        admission_failure(probe, keys[0])
        report['cases'].append('injected_15s_admission_and_single_manual_retry_passed')
        twins = [Client(server, rows, probe.cookie) for _ in range(2)]
        for twin in twins: twin.read()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c:c.request('/api/collect',{'qr':keys[1]}),twins))
        require(sorted(r['added'] for r in results)==[False,True] and probe.read()['customer']['count']==2, 'same_product_race_failed')
        report['cases'].append('new_product_parallel_duplicate_http_passed')
        before_restart = logical_state(root)
        server.stop(); server.start()
        require(logical_state(root)==before_restart and restored.read()['customer']['status']=='redeemed', 'restart_lost_records')
        report['cases'].append('waitress_stop_restart_records_preserved')
        baseline = logical_state(root)
        backup = ops.backup(root)
        probe.collect(keys[2])
        current = logical_state(root)
        candidate = ops.stage_restore(root, backup['daily'])['candidate']
        require(logical_state(root)==current and logical_state(root,candidate)==baseline, 'restore_stage_changed_current_or_candidate')
        server.stop()
        (root/'config/maintenance.flag').touch()
        result = ops.activate_restore(root,Path(candidate),True)
        require(Path(result['retired']).is_dir(), 'original_not_preserved')
        (root/'config/maintenance.flag').unlink()
        server.start()
        require(logical_state(root)==baseline and original_products==logical_state(root)['products'], 'restore_mismatch')
        require(probe.read()['customer']['count']==2 and restored.read()['customer']['status']=='redeemed', 'restored_http_state_failed')
        require(ops.selftest(root)['selftest']=='ok', 'existing_http_selftest_failed')
        report['cases'].append('backup_stage_original_preservation_restore_T0_passed')
        report['audit'] = audit(root)
        require(report['audit']['customers']==47 and report['audit']['stamps']==237 and report['audit']['reward_audit']==1, 'final_counts_mismatch')
        report['tool_checks_passed'] = True
    except BaseException as error:
        report['failure'] = error.args[0] if isinstance(error,CheckFailed) else type(error).__name__
        raise
    finally:
        try:
            if server:
                server.stop()
        except BaseException as cleanup:
            report['tool_checks_passed'] = False
            report['cleanup_failure'] = type(cleanup).__name__
            raise
        finally:
            report['http'] = summarize(rows)
            ops.write_json(root/'field-tool-report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    settings = commands.add_parser('prepare', help='New synthetic settings only; no DB/server/TLS')
    settings.add_argument('--out', required=True)
    settings.add_argument('--origin', required=True)
    settings.add_argument('--port', type=int, default=8875)
    settings.add_argument('--metrics-port', type=int, default=20242)
    check = commands.add_parser('run', help='New private fixture, actual loopback Waitress, no external target')
    check.add_argument('--out', required=True)
    check.add_argument('--tooling-smoke', action='store_true', help='Non-Windows tool check only; not runtime/field approval')
    args = parser.parse_args()
    os.umask(0o077)
    logging.getLogger('waitress').setLevel(logging.ERROR)
    try:
        if args.command=='prepare':
            prepare(args.out,args.origin,args.port,args.metrics_port)
            print('FIELD_SETTINGS_CREATED no_database_no_server_no_TLS')
        else:
            report = run(args.out,args.tooling_smoke)
            print(json.dumps({'tool_checks_passed': report['tool_checks_passed'], 'field_acceptance':'not_performed'}))
        return 0
    except BaseException as error:
        label = error.args[0] if isinstance(error,CheckFailed) else type(error).__name__
        print('FIELD_TOOL_FAILED '+str(label), file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
