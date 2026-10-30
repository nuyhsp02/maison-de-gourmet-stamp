"""Opt-in A/B experiment. Never imported by application or deployment code.

All exported observations contain fixed labels/counts/timings, not request data.
B is a connection adapter prototype, NOT a production configuration switch.
"""
import inspect
import math
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from flask import request


def statistics(values):
    values = sorted(values)
    if not values:
        return {'n': 0, 'total_ms': 0, 'mean_ms': None, 'max_ms': None, 'p95_ms': None, 'p99_ms': None}
    return {'n': len(values), 'total_ms': round(sum(values) * 1000, 3), 'mean_ms': round(sum(values) / len(values) * 1000, 3),
            'max_ms': round(values[-1] * 1000, 3),
            'p95_ms': round(values[math.ceil(len(values) * .95) - 1] * 1000, 3),
            'p99_ms': round(values[math.ceil(len(values) * .99) - 1] * 1000, 3)}


class Profile:
    def __init__(self, enabled):
        self.enabled = enabled
        self.local = threading.local()
        self.records = deque()

    @contextmanager
    def stage(self, label):
        record = getattr(self.local, 'record', None)
        if not self.enabled or record is None:
            yield
            return
        stack = self.local.stack
        if stack and stack[-1][0] == label:
            yield
            return
        frame = [label, 0.0]
        stack.append(frame)
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            stack.pop()
            record['inclusive'][label] += elapsed
            record['exclusive'][label] += max(0, elapsed - frame[1])
            if stack:
                stack[-1][1] += elapsed

    def wrapper(self, label, function):
        @wraps(function)
        def measured(*args, **kwargs):
            with self.stage(label):
                return function(*args, **kwargs)
        return measured

    def install(self, app, patch):
        if not self.enabled:
            return
        original_wsgi = app.wsgi_app
        def measured_wsgi(environ, start_response):
            record = {'method': environ['REQUEST_METHOD'], 'status': None,
                      'inclusive': defaultdict(float), 'exclusive': defaultdict(float), 'holds': []}
            self.local.record, self.local.stack = record, []
            started = time.perf_counter()
            def response(status, headers, exc_info=None):
                record['status'] = int(status.split()[0])
                return start_response(status, headers, exc_info)
            try:
                return original_wsgi(environ, response)
            finally:
                record['total'] = time.perf_counter() - started
                record['exclusive']['other'] = max(0, record['total'] - sum(record['exclusive'].values()))
                self.records.append(record)
                self.local.record = None
        patch.setattr(app, 'wsgi_app', measured_wsgi)
        # Wrap the actual nested functions via their closure cells; no copied route
        # logic or trace/profile callbacks on every Python call.
        labels = {name: 'validation' for name in
                  ('check_request', 'identity', 'require_identity', 'body', 'require_active', 'limited')}
        labels['snapshot'] = 'snapshot'
        seen, cells, replacements = set(), [], {}
        def walk(function):
            if not inspect.isfunction(function) or function.__module__ != 'app' or id(function) in seen:
                return
            seen.add(id(function))
            if function.__name__ in labels:
                replacements[function] = self.wrapper(labels[function.__name__], function)
            for cell in function.__closure__ or ():
                value = cell.cell_contents
                if inspect.isfunction(value) and value.__module__ == 'app':
                    cells.append((cell, value))
                    walk(value)
        roots = list(app.view_functions.values()) + app.before_request_funcs.get(None, [])
        for function in roots:
            walk(function)
        changed = set()
        for cell, value in cells:
            if value in replacements and id(cell) not in changed:
                changed.add(id(cell))
                patch.setattr(cell, 'cell_contents', replacements[value])
        patch.setattr(app, 'before_request_funcs', {
            **app.before_request_funcs,
            None: [replacements.get(fn, fn) for fn in app.before_request_funcs.get(None, [])]})

    def summary(self):
        posts = [r for r in self.records if r['method'] == 'POST']
        labels = ('lock_wait', 'validation', 'db_connect', 'pragma', 'begin', 'write_sql',
                  'commit', 'rollback', 'db_read', 'snapshot', 'close', 'lock_release', 'other')
        return {'post_total': statistics([r['total'] for r in posts]),
                'lock_hold': statistics([sum(r['holds']) for r in posts if r['holds']]),
                'segments': {label: {
                    'exclusive': statistics([r['exclusive'][label] for r in posts if label in r['exclusive']]),
                    'inclusive': statistics([r['inclusive'][label] for r in posts if label in r['inclusive']])}
                    for label in labels},
                'slow_posts': [{'status': r['status'], 'total_ms': round(r['total'] * 1000, 3),
                                'hold_ms': round(sum(r['holds']) * 1000, 3),
                                'exclusive_ms': {k: round(v * 1000, 3) for k, v in r['exclusive'].items()}}
                               for r in sorted(posts, key=lambda r: r['total'], reverse=True)[:8]],
                'slow_holds': [{'status': r['status'], 'total_ms': round(r['total'] * 1000, 3),
                                'hold_ms': round(sum(r['holds']) * 1000, 3),
                                'exclusive_ms': {k: round(v * 1000, 3) for k, v in r['exclusive'].items()}}
                               for r in sorted(posts, key=lambda r: sum(r['holds']), reverse=True)[:8]]}


class TimedLock:
    def __init__(self, lock, profile):
        self.lock, self.profile = lock, profile
        self.local = threading.local()

    def acquire(self, blocking=True, timeout=-1):
        with self.profile.stage('lock_wait'):
            acquired = self.lock.acquire(blocking, timeout)
        if acquired:
            self.local.started = time.perf_counter()
        return acquired

    def release(self):
        record = getattr(self.profile.local, 'record', None)
        if record is not None:
            record['holds'].append(time.perf_counter() - self.local.started)
        with self.profile.stage('lock_release'):
            self.lock.release()


class OuterBypass:
    """B only: leave real request validation intact; move ownership into SQL adapter."""
    def acquire(self, blocking=True, timeout=-1):
        return True

    def release(self):
        pass


class Cursor:
    def __init__(self, raw, profile):
        self.raw, self.profile = raw, profile

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def fetchone(self):
        with self.profile.stage('db_read'):
            return self.raw.fetchone()

    def fetchall(self):
        with self.profile.stage('db_read'):
            return self.raw.fetchall()


class Connection:
    def __init__(self, raw, profile, gate, app, validate=None):
        self.raw, self.profile, self.gate, self.app = raw, profile, gate, app
        self.validate = validate
        self.owns_gate = False

    @property
    def row_factory(self):
        return self.raw.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.raw.row_factory = value

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        try:
            with self.profile.stage('rollback' if args[0] is not None else 'commit'):
                return self.raw.__exit__(*args)
        finally:
            if not self.raw.in_transaction:
                self.release()

    def acquire(self):
        from app import APIError
        if self.gate is not None and not self.owns_gate:
            if not self.gate.acquire(timeout=15):
                self.app.logger.warning('Write admission timed out')
                raise APIError('잠시 후 다시 시도하세요. 서버가 기록을 처리하고 있습니다.',
                               503, 'storage_unavailable')
            self.owns_gate = True
            # An authenticated request can wait while another request revokes
            # its session. Reuse actual validation under ownership, before SQL.
            if self.validate is not None and request.endpoint in ('collect', 'redeem', 'recovery_link'):
                self.validate()

    def release(self):
        if self.owns_gate:
            self.owns_gate = False
            self.gate.release()

    def execute(self, statement, *args, **kwargs):
        # Inspect only the verb, never retain or output SQL/parameters.
        verb = statement.lstrip().split(None, 1)[0].upper()
        label = {'PRAGMA': 'pragma', 'BEGIN': 'begin', 'INSERT': 'write_sql',
                 'UPDATE': 'write_sql', 'DELETE': 'write_sql'}.get(verb, 'db_read')
        standalone = self.gate is not None and label == 'write_sql' and not self.raw.in_transaction
        if verb == 'BEGIN' or standalone:
            self.acquire()
        try:
            with self.profile.stage(label):
                cursor = self.raw.execute(statement, *args, **kwargs)
            return Cursor(cursor, self.profile)
        finally:
            if standalone:
                self.release()

    def commit(self):
        with self.profile.stage('commit'):
            self.raw.commit()
        # A failed commit keeps ownership until rollback/close completes.
        self.release()

    def rollback(self):
        with self.profile.stage('rollback'):
            self.raw.rollback()
        self.release()

    def close(self):
        try:
            with self.profile.stage('close'):
                self.raw.close()
        finally:
            self.release()


def closure_function(app, name):
    pending, seen = list(app.view_functions.values()), set()
    while pending:
        function = pending.pop()
        if not inspect.isfunction(function) or id(function) in seen:
            continue
        seen.add(id(function))
        if function.__name__ == name:
            return function
        pending.extend(cell.cell_contents for cell in function.__closure__ or ()
                       if inspect.isfunction(cell.cell_contents))
    raise AssertionError('Required validation function not found')


@contextmanager
def experiment(app, monkeypatch, variant, profiled):
    import app as module
    if variant not in ('A', 'B'):
        raise ValueError('Only A/B are implemented')
    profile = Profile(profiled)
    with monkeypatch.context() as patch:
        gate = app.write_lock
        if profiled:
            gate = TimedLock(gate, profile)
        if variant == 'B':
            patch.setattr(app, 'write_lock', OuterBypass())
        elif profiled:
            patch.setattr(app, 'write_lock', gate)
        profile.install(app, patch)
        validate = closure_function(app, 'require_identity') if variant == 'B' else None
        # A without profiling is the actual app with native Lock/SQLite connections.
        if profiled or variant == 'B':
            original_connect = sqlite3.connect
            def connected(*args, **kwargs):
                with profile.stage('db_connect'):
                    raw = original_connect(*args, **kwargs)
                return Connection(raw, profile, gate if variant == 'B' else None, app, validate)
            patch.setattr(module.sqlite3, 'connect', connected)
        yield profile, gate


def audit_database(path, ledger):
    """Read-only audit; private synthetic session/QR values remain in memory."""
    from contextlib import closing
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        sessions = dict(db.execute('SELECT token_hash, customer_id FROM sessions'))
        products = dict(db.execute('SELECT qr_key, id FROM products'))
        stamps = set(db.execute('SELECT customer_id, product_id FROM stamps'))
        rewards = dict(db.execute('SELECT customer_id, redeemed_at FROM reward_audit'))
        customers = dict(db.execute('SELECT id, redeemed_at FROM customers'))
        missing_sessions = sum(token not in sessions for kind, token, _ in ledger if kind == 'session')
        missing_stamps = sum((sessions.get(token), products.get(product)) not in stamps
                             for kind, token, product in ledger if kind == 'stamp')
        missing_rewards = sum(sessions.get(token) not in rewards for kind, token, _ in ledger if kind == 'reward')
        reward_mismatch = sum(customers.get(cid) != when for cid, when in rewards.items())
        redeemed_without_audit = sum(when is not None and cid not in rewards for cid, when in customers.items())
        incomplete_rewards = sum(sum(s[0] == cid for s in stamps) != 10 for cid in rewards)
        result = {'integrity_ok': db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                  'foreign_key_errors': len(db.execute('PRAGMA foreign_key_check').fetchall()),
                  'customers': len(customers), 'stamps': len(stamps), 'rewards': len(rewards),
                  'duplicate_stamps': db.execute('SELECT COUNT(*) FROM (SELECT customer_id, product_id FROM stamps GROUP BY customer_id, product_id HAVING COUNT(*)>1)').fetchone()[0],
                  'duplicate_rewards': db.execute('SELECT COUNT(*) FROM (SELECT customer_id FROM reward_audit GROUP BY customer_id HAVING COUNT(*)>1)').fetchone()[0],
                  'missing_ack_sessions': missing_sessions, 'missing_ack_stamps': missing_stamps,
                  'missing_ack_rewards': missing_rewards, 'reward_mismatch': reward_mismatch,
                  'redeemed_without_audit': redeemed_without_audit, 'incomplete_rewards': incomplete_rewards}
        result['ok'] = result['integrity_ok'] and not any(result[k] for k in result if k not in
                         ('integrity_ok', 'customers', 'stamps', 'rewards'))
        return result
