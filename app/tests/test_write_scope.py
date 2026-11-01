"""Real production B, including races and cleanup faults; no prototype adapter."""
import json
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import request
from conftest import Visitor


@pytest.mark.parametrize('case', ['session_revoked', 'collect_ended', 'session_ended', 'recovery_rotated'])
def test_conditions_changed_before_admission(app, keys, monkeypatch, case):
    visitor = Visitor(app)
    link = visitor.post('/api/recovery-link', {'confirm_rotation': True}).json['url'].split('#')[1]
    stale = Visitor(app, cookie=visitor.cookie())
    endpoint = 'recover' if case == 'recovery_rotated' else ('start' if case == 'session_ended' else 'collect')
    ready, resume = threading.Event(), threading.Event()
    native = app.write_lock
    class Paused:
        def acquire(self, blocking=True, timeout=-1):
            if request.endpoint == endpoint and threading.current_thread().name.startswith('racer'):
                ready.set()
                assert resume.wait(10)
            return native.acquire(blocking, timeout)
        def release(self):
            native.release()
    monkeypatch.setattr(app, 'write_lock', Paused())
    if case == 'session_ended':
        stale = Visitor(app, start=False)
    path = '/api/recover' if case == 'recovery_rotated' else ('/api/session' if case == 'session_ended' else '/api/collect')
    body = {'token': link} if case == 'recovery_rotated' else ({} if case == 'session_ended' else {'qr': keys[0]})
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='racer') as pool:
        future = pool.submit(stale.post, path, body)
        try:
            assert ready.wait(10)
            if case == 'session_revoked':
                assert visitor.post('/api/recover', {'token': link}).status_code == 200
            elif case == 'recovery_rotated':
                assert visitor.post('/api/recovery-link', {'confirm_rotation': True}).status_code == 200
            else:
                app.event.update(draft=False, starts_at='2000-01-01T00:00:00+00:00',
                                 ends_at='2000-01-02T00:00:00+00:00', reward_until='2099-01-01T00:00:00+00:00')
        finally:
            resume.set()
        expected = 401 if case == 'session_revoked' else (400 if case == 'recovery_rotated' else 409)
        assert future.result(timeout=10).status_code == expected


@pytest.mark.parametrize('fault', ['begin', 'commit', 'rollback', 'close', 'cancel'])
def test_transaction_cleanup_faults(app, monkeypatch, fault):
    import app as module
    original = module.connect_db
    opened = []
    class Connection:
        def __init__(self, raw):
            self.raw = raw
            opened.append(raw)
        def __getattr__(self, name):
            return getattr(self.raw, name)
        def execute(self, sql, *args):
            if fault == 'begin' and sql == 'BEGIN IMMEDIATE':
                assert app.write_lock.locked()
                raise sqlite3.OperationalError('synthetic begin failure')
            return self.raw.execute(sql, *args)
        def commit(self):
            assert app.write_lock.locked()
            if fault == 'cancel':
                raise KeyboardInterrupt()
            raise sqlite3.OperationalError('synthetic commit failure')
        def rollback(self):
            assert app.write_lock.locked()
            if fault in ('rollback', 'close'):
                raise sqlite3.OperationalError('synthetic rollback failure')
            self.raw.rollback()
        def close(self):
            if fault in ('rollback', 'close'):
                assert app.write_lock.locked()
            if fault == 'close':
                raise sqlite3.OperationalError('synthetic close failure')
            self.raw.close()
    with monkeypatch.context() as patch:
        patch.setattr(module, 'connect_db', lambda path: Connection(original(path)))
        if fault == 'cancel':
            with pytest.raises(KeyboardInterrupt):
                Visitor(app)
        else:
            client = Visitor(app, start=False)
            assert client.post('/api/session', {}).status_code == 503
    assert not app.write_lock.locked()
    for connection in opened:
        connection.close()
    if fault == 'close':
        assert Visitor(app, start=False).post('/api/session', {}).status_code == 503
    else:
        assert Visitor(app).read()['customer']['count'] == 0
    with original(app.config['DATABASE']) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == (0 if fault == 'close' else 1)


def test_snapshot_queries_are_one_read_view_and_close_is_outside_lock(app, keys, monkeypatch):
    import app as module
    original = module.connect_db
    observer = Visitor(app)
    cookie = observer.cookie()
    fired = False
    class Connection:
        def __init__(self, raw): self.raw = raw
        def __getattr__(self, name): return getattr(self.raw, name)
        def execute(self, sql, *args):
            nonlocal fired
            cursor = self.raw.execute(sql, *args)
            if sql.startswith('SELECT * FROM customers') and not fired:
                fired = True
                assert not app.write_lock.locked()
                # Finish a concurrent collection after the customer SELECT has
                # established the reader's snapshot, before its stamp SELECT.
                with ThreadPoolExecutor(max_workers=1) as pool:
                    assert pool.submit(lambda: Visitor(app, cookie=cookie).collect(keys[0])).result()['added']
            return cursor
        def close(self):
            assert not app.write_lock.locked()
            self.raw.close()
    with monkeypatch.context() as patch:
        patch.setattr(module, 'connect_db', lambda path: Connection(original(path)))
        assert observer.read()['customer']['count'] == 0
    assert observer.read()['customer']['count'] == 1


def test_original_load_with_lock_timings(app, keys, monkeypatch):
    from test_load import test_400_visitors_30_concurrent
    native = app.write_lock
    waits, holds = deque(), deque()
    local = threading.local()
    class Timed:
        def acquire(self, blocking=True, timeout=-1):
            start = time.perf_counter()
            ok = native.acquire(blocking, timeout)
            waits.append(time.perf_counter() - start)
            if ok: local.start = time.perf_counter()
            return ok
        def release(self):
            holds.append(time.perf_counter() - local.start)
            native.release()
    monkeypatch.setattr(app, 'write_lock', Timed())
    try:
        test_400_visitors_30_concurrent(app, keys)
        assert len(holds) == 5200
    finally:
        def stats(values):
            import math
            values = sorted(values)
            return {k: round(v*1000, 3) for k,v in {
                'mean_ms': sum(values)/len(values), 'max_ms': values[-1],
                'p95_ms': values[math.ceil(len(values)*.95)-1],
                'p99_ms': values[math.ceil(len(values)*.99)-1]}.items()} if values else None
        print('B_LOCK_TIMINGS ' + json.dumps({'wait': stats(waits), 'hold': stats(holds),
                                             'grants': len(holds)}))


def test_collect_and_redeem_collision_keeps_single_reward(app, keys):
    from app import connect_db
    from contextlib import closing
    visitor = Visitor(app)
    for key in keys[:-1]: visitor.collect(key)
    cookie = visitor.cookie()
    def collect(): return Visitor(app, cookie=cookie).collect(keys[-1])
    def redeem(): return Visitor(app, cookie=cookie).post('/api/redeem', {'confirm_received': True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(collect)
        response = pool.submit(redeem).result()
        assert pending.result()['added']
    assert response.status_code in (200, 409)
    assert visitor.post('/api/redeem', {'confirm_received': True}).status_code == 200
    with closing(connect_db(app.config['DATABASE'])) as db:
        assert db.execute('SELECT COUNT(*) FROM stamps').fetchone()[0] == 10
        assert db.execute('SELECT COUNT(*) FROM reward_audit').fetchone()[0] == 1


def test_real_admission_deadline_and_recovery(app, keys):
    visitor = Visitor(app)
    assert app.write_lock.acquire(timeout=1)
    started = time.monotonic()
    try:
        response = visitor.post('/api/collect', {'qr': keys[0]})
        assert response.status_code == 503 and response.json['code'] == 'storage_unavailable'
        assert time.monotonic() - started >= 14.9
    finally:
        app.write_lock.release()
    assert visitor.collect(keys[0])['added']


def test_connection_setup_failure_closes_handle(monkeypatch):
    import app as module
    class Broken:
        closed = False
        def execute(self, _sql): raise sqlite3.OperationalError('synthetic pragma error')
        def close(self): self.closed = True
    raw = Broken()
    monkeypatch.setattr(module.sqlite3, 'connect', lambda *a, **k: raw)
    with pytest.raises(sqlite3.OperationalError): module.connect_db('unused-test-path')
    assert raw.closed
