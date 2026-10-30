"""Manual Windows A/B matrix; the original 400/30 function is not edited."""
import json
import os
import time
from collections import Counter, deque
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import Visitor
from concurrency_support import failure_counts, runtime_info
from scope_experiment import Connection, Profile, audit_database, experiment, statistics


def test_statistics_use_nearest_rank_and_no_false_zero():
    assert statistics([])['mean_ms'] is None
    result = statistics([i / 1000 for i in range(1, 101)])
    assert (result['mean_ms'], result['max_ms'], result['p95_ms'], result['p99_ms']) == (50.5, 100, 95, 99)


def test_b_commit_failure_keeps_gate_until_rollback(app):
    import sqlite3
    import threading
    class FailedCommit:
        in_transaction = True
        def commit(self):
            raise sqlite3.OperationalError('synthetic')
        def rollback(self):
            self.in_transaction = False
    gate = threading.Lock()
    connection = Connection(FailedCommit(), Profile(False), gate, app)
    connection.acquire()
    with pytest.raises(sqlite3.OperationalError):
        connection.commit()
    assert gate.locked()
    connection.rollback()
    assert not gate.locked()


def test_b_duplicate_recovery_reward_and_failure_recovery(app, keys, monkeypatch):
    from app import connect_db
    with experiment(app, monkeypatch, 'B', False) as (_, gate):
        visitor = Visitor(app)
        recovery = visitor.post('/api/recovery-link', {'confirm_rotation': True})
        assert recovery.status_code == 200
        token = recovery.json['url'].split('#')[1]
        restored = Visitor(app, start=False)
        assert restored.post('/api/recover', {'token': token}).status_code == 200
        restored.read()
        cookie = restored.cookie()
        def collect_duplicate(_):
            return Visitor(app, cookie=cookie).collect(keys[0])['added']
        with ThreadPoolExecutor(max_workers=30) as pool:
            assert sum(pool.map(collect_duplicate, range(30))) == 1
        for key in keys[1:]:
            restored.collect(key)
        def redeem(_):
            response = Visitor(app, cookie=cookie).post('/api/redeem', {'confirm_received': True})
            assert response.status_code == 200
            return not response.json['already_redeemed']
        with ThreadPoolExecutor(max_workers=30) as pool:
            assert sum(pool.map(redeem, range(30))) == 1
        assert restored.read()['customer']['status'] == 'redeemed'
        assert not restored.collect(keys[0])['added']
        # APIError inside BEGIN must roll back on close and release ownership.
        bad = Visitor(app, start=False).post('/api/recover', {'token': 'a' * 43})
        assert bad.status_code == 400
        assert gate.acquire(timeout=1)
        gate.release()
    with closing(connect_db(app.config['DATABASE'])) as db:
        assert db.execute('SELECT COUNT(*) FROM stamps').fetchone()[0] == 10
        assert db.execute('SELECT COUNT(*) FROM reward_audit').fetchone()[0] == 1
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'



@pytest.mark.parametrize('variant', ['A', 'B'])
def test_profile_covers_actual_request_segments(app, keys, monkeypatch, variant):
    with experiment(app, monkeypatch, variant, True) as (profile, _):
        visitor = Visitor(app)
        visitor.collect(keys[0])
        response = visitor.post('/api/recover', {'token': 'a' * 43})
        assert response.status_code == 400
    result = profile.summary()
    assert result['post_total']['n'] == 3
    for label in ('lock_wait', 'validation', 'db_connect', 'pragma', 'begin', 'write_sql',
                  'commit', 'rollback', 'snapshot', 'close', 'lock_release'):
        assert result['segments'][label]['exclusive']['n'] > 0, label
    assert result['lock_hold']['n'] == 3
    assert all(r['total_ms'] >= sum(r['exclusive_ms'].values()) - .02 for r in result['slow_posts'])


def test_b_rechecks_session_revoked_while_waiting(app, keys, monkeypatch):
    import threading
    from flask import request
    ready, resume = threading.Event(), threading.Event()
    with experiment(app, monkeypatch, 'B', False):
        visitor = Visitor(app)
        token = visitor.post('/api/recovery-link', {'confirm_rotation': True}).json['url'].split('#')[1]
        stale = Visitor(app, cookie=visitor.cookie())
        original_acquire = Connection.acquire
        def pause_before_gate(connection):
            if request.endpoint == 'collect':
                ready.set()
                assert resume.wait(5)
            return original_acquire(connection)
        with monkeypatch.context() as patch:
            patch.setattr(Connection, 'acquire', pause_before_gate)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(stale.post, '/api/collect', {'qr': keys[0]})
                try:
                    assert ready.wait(5)
                    assert visitor.post('/api/recover', {'token': token}).status_code == 200
                finally:
                    resume.set()
                assert pending.result(timeout=5).status_code == 401
        assert visitor.read()['customer']['count'] == 0


def test_audit_detects_missing_acknowledged_stamp(app, keys):
    from app import digest
    visitor = Visitor(app)
    ledger = [('session', digest(visitor.cookie()), None), ('stamp', digest(visitor.cookie()), keys[0])]
    before = audit_database(app.config['DATABASE'], ledger)
    assert not before['ok'] and before['missing_ack_stamps'] == 1
    visitor.collect(keys[0])
    assert audit_database(app.config['DATABASE'], ledger)['ok']


def test_failed_workload_still_audits_and_preserves_assertion(app, keys, monkeypatch, caplog, capsys):
    class Denied:
        def acquire(self, blocking=True, timeout=-1):
            assert timeout == 15
            return False
        def release(self):
            raise AssertionError('Unowned lock')
    monkeypatch.setattr(app, 'write_lock', Denied())
    monkeypatch.setenv('MAISON_SCOPE_VARIANT', 'A')
    monkeypatch.setenv('MAISON_SCOPE_PROFILE', '0')
    with pytest.raises(AssertionError):
        test_scope_original_400_30(app, keys, monkeypatch, caplog)
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith('SCOPE_EXPERIMENT '))
    result = json.loads(line.split(' ', 1)[1])
    assert not result['original_workload_completed']
    assert result['audit']['ok'] and result['post_responses']['503'] > 0

@pytest.mark.skipif(os.environ.get('MAISON_SCOPE_VARIANT') not in ('A', 'B'),
                    reason='Opt-in manual A/B experiment only')
def test_scope_original_400_30(app, keys, monkeypatch, caplog):
    import test_load
    from app import digest
    variant = os.environ['MAISON_SCOPE_VARIANT']
    profiled = os.environ.get('MAISON_SCOPE_PROFILE') == '1'
    ledger, responses = deque(), deque()
    class AcknowledgedVisitor(Visitor):
        def post(self, path, body):
            response = super().post(path, body)
            responses.append(response.status_code)
            if response.status_code == 200:
                token_hash = digest(self.cookie())
                data = response.json
                if path == '/api/session':
                    ledger.append(('session', token_hash, None))
                elif path == '/api/collect' and data['added']:
                    ledger.append(('stamp', token_hash, body['qr']))
                elif path == '/api/redeem' and not data['already_redeemed']:
                    ledger.append(('reward', token_hash, None))
            return response
    completed = False
    started = time.perf_counter()
    profile = None
    try:
        with experiment(app, monkeypatch, variant, profiled) as (profile, _):
            with monkeypatch.context() as patch:
                patch.setattr(test_load, 'Visitor', AcknowledgedVisitor)
                test_load.test_400_visitors_30_concurrent(app, keys)
                completed = True
    finally:
        elapsed = time.perf_counter() - started
        # Runs even when the unchanged original assertion fails. All workers have
        # joined before this finally; hard CI termination may prevent the audit.
        audit = audit_database(app.config['DATABASE'], ledger)
        result = {'variant': variant, 'profiled': profiled, 'runtime': runtime_info(),
                  'original_workload_completed': completed, 'elapsed_s': round(elapsed, 3),
                  'post_responses': dict(Counter(responses)),
                  'post_failure_rate': round(sum(s != 200 for s in responses) / max(1, len(responses)), 6),
                  'observed_posts_per_s': round(len(responses) / elapsed, 3),
                  'acknowledged': dict(Counter(row[0] for row in ledger)), 'audit': audit,
                  **failure_counts(caplog.get_records('call'))}
        if profiled:
            result['timing'] = profile.summary()
        print('SCOPE_EXPERIMENT ' + json.dumps(result), flush=True)
        assert audit['ok'], {k: v for k, v in audit.items() if k != 'ok'}
