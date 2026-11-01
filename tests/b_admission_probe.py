"""Opt-in pytest diagnostics; never imported by the operating application.

The original raw load remains untouched. Only its existing timed twin receives
connection/native-lock observers. Timings are observations, not scheduler-neutral.
"""
import json
from bisect import bisect_left
import math
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

RAW = 'test_400_visitors_30_concurrent'
TIMED = 'test_original_load_with_lock_timings'


def stats(values):
    values = sorted(values)
    if not values:
        return None
    return dict(mean_ms=round(sum(values) / len(values) * 1000, 3),
                max_ms=round(values[-1] * 1000, 3),
                p95_ms=round(values[math.ceil(len(values) * .95) - 1] * 1000, 3),
                p99_ms=round(values[math.ceil(len(values) * .99) - 1] * 1000, 3))


def overlap(a, b, c, d):
    return max(0, min(b, d) - max(a, c))


class Probe:
    def __init__(self, native):
        self.native = native
        self.guard = threading.Lock()
        self.local = threading.local()
        self.waits, self.holds, self.phases, self.responses = [], [], [], []
        self.errors = Counter()
        self.pending = self.peak_pending = 0
        self.started = time.perf_counter()

    def now(self):
        return time.perf_counter() - self.started

    def acquire(self, blocking=True, timeout=-1):
        start = self.now()
        with self.guard:
            self.pending += 1
            self.peak_pending = max(self.peak_pending, self.pending)
        ok = self.native.acquire(blocking, timeout)
        end = self.now()
        with self.guard:
            self.pending -= 1
            self.waits.append((start, end, ok))
        if ok:
            self.local.acquired = end
            self.local.owned = True
        return ok

    def release(self):
        end = self.now()
        self.holds.append((self.local.acquired, end))
        self.local.owned = False
        self.native.release()

    def locked(self):
        return self.native.locked()

    def call(self, phase, function, *args, **kwargs):
        start = self.now()
        owned = getattr(self.local, 'owned', False)
        try:
            return function(*args, **kwargs)
        except sqlite3.OperationalError:
            with self.guard:
                self.errors[phase] += 1
            raise
        finally:
            self.phases.append((phase, start, self.now(), owned))

    def summary(self):
        by_phase = {}
        for name in sorted({row[0] for row in self.phases}):
            rows = [row for row in self.phases if row[0] == name]
            by_phase[name] = dict(count=len(rows), **stats([e-s for _,s,e,_ in rows]))
        slow = []
        for start, end, ok in sorted(self.waits, key=lambda x: x[1]-x[0], reverse=True)[:8]:
            held = sum(overlap(start, end, s, e) for s,e in self.holds)
            commits = sum(overlap(start,end,s,e) for n,s,e,_ in self.phases if n == 'commit')
            closes = sum(overlap(start,end,s,e) for n,s,e,owned in self.phases if n == 'close' and not owned)
            slow.append(dict(wait_ms=round((end-start)*1000,3), admitted=ok,
                             other_grants=sum(start < s < end for s,_ in self.holds),
                             later_arrivals_granted=sum(start < s and e < end and admitted for s,e,admitted in self.waits),
                             successful_write_responses=sum(start < t < end and code == 200 for t,code in self.responses),
                             held_wall_ms=round(held*1000,3), commit_wall_ms=round(commits*1000,3),
                             outside_close_sum_ms=round(closes*1000,3)))
        outside_closes = [(s,e) for n,s,e,owned in self.phases if n == 'close' and not owned]
        ordered_closes = sorted(outside_closes)
        close_starts, close_end_max = [], []
        for start, end in ordered_closes:
            close_starts.append(start)
            close_end_max.append(max(end, close_end_max[-1] if close_end_max else end))
        def has_close(start, end):
            index = bisect_left(close_starts, end) - 1
            return index >= 0 and close_end_max[index] > start
        elapsed = self.now()
        slow_holds = [dict(hold_ms=round((e-s)*1000,3),
                           commit_ms=round(sum(overlap(s,e,a,b) for n,a,b,_ in self.phases if n == 'commit')*1000,3),
                           outside_close_sum_ms=round(sum(overlap(s,e,a,b) for a,b in outside_closes)*1000,3))
                      for s,e in sorted(self.holds, key=lambda row: row[1]-row[0], reverse=True)[:8]]
        return dict(elapsed_seconds=round(elapsed,3), grants=len(self.holds),
                    timeouts=sum(not ok for _,_,ok in self.waits), peak_waiters=self.peak_pending,
                    wait=stats([e-s for s,e,_ in self.waits]), hold=stats([e-s for s,e in self.holds]),
                    phases=by_phase, slow_waits=slow, slow_holds=slow_holds, operational_errors=dict(self.errors),
                    successful_write_responses=sum(code == 200 for _,code in self.responses),
                    response_status_counts=dict(Counter(str(code) for _,code in self.responses)),
                    completed_write_responses_per_second=round(len(self.responses)/elapsed,3),
                    holds_overlapping_outside_close=sum(has_close(s,e) for s,e in self.holds),
                    outside_close_note='overlap is correlation; internal checkpoint not directly observed')


def observe_connection(raw, probe):
    class Connection:
        def __getattr__(self, name):
            return getattr(raw, name)
        def execute(self, sql, *args, **kwargs):
            # Classify only; never store SQL, parameters, customers or tokens.
            upper = sql.lstrip().upper()
            phase = ('begin' if upper == 'BEGIN IMMEDIATE' else
                     'pragma' if upper.startswith('PRAGMA') else
                     'write_sql' if upper.startswith(('INSERT', 'UPDATE', 'DELETE')) else 'read_sql')
            return probe.call(phase, raw.execute, sql, *args, **kwargs)
        def commit(self):
            return probe.call('commit', raw.commit)
        def rollback(self):
            return probe.call('rollback', raw.rollback)
        def close(self):
            return probe.call('close', raw.close)
        def __enter__(self):
            raw.__enter__()
            return self
        def __exit__(self, *args):
            return raw.__exit__(*args)
    return Connection()


def audit(path):
    with closing(sqlite3.connect(path)) as db:
        counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('customers', 'stamps', 'reward_audit')}
        counts.update(integrity_ok=db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                      foreign_keys_ok=not db.execute('PRAGMA foreign_key_check').fetchall(),
                      duplicate_stamps=db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM stamps GROUP BY customer_id,product_id HAVING COUNT(*)>1)').fetchone()[0],
                      duplicate_rewards=db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM reward_audit GROUP BY customer_id HAVING COUNT(*)>1)').fetchone()[0],
                      invalid_rewards=db.execute('SELECT COUNT(*) FROM customers c WHERE (c.redeemed_at IS NOT NULL) != EXISTS(SELECT 1 FROM reward_audit a WHERE a.customer_id=c.id) OR (c.redeemed_at IS NOT NULL AND (SELECT COUNT(*) FROM stamps s WHERE s.customer_id=c.id)!=10)').fetchone()[0])
        return counts


class Plugin:
    def __init__(self, output):
        self.output, self.records = Path(output), []

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        if item.name not in (RAW, TIMED):
            yield
            return
        app = item.funcargs['app']
        probe = None
        patch = pytest.MonkeyPatch()
        if item.name == TIMED:
            import app as module
            probe = Probe(app.write_lock)
            patch.setattr(app, 'write_lock', probe)
            original = module.connect_db
            def connect(path):
                # Includes the existing initial PRAGMAs; setup isn't a lock hold.
                raw = probe.call('connect_and_initial_pragmas', original, path)
                return observe_connection(raw, probe)
            patch.setattr(module, 'connect_db', connect)
            from flask import request
            @app.after_request
            def response(response):
                if request.method == 'POST':
                    probe.responses.append((probe.now(), response.status_code))
                return response
        started = time.perf_counter()
        outcome = yield
        elapsed = time.perf_counter() - started
        patch.undo()
        record = dict(test=item.name, instrumented=probe is not None,
                      failed=outcome.excinfo is not None, elapsed_seconds=round(elapsed,3),
                      audit=audit(app.config['DATABASE']))
        if probe:
            record['probe'] = probe.summary()
        self.records.append(record)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.records, indent=2), encoding='utf-8')
        print('B_ADMISSION_DIAGNOSTIC ' + json.dumps(record))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    # Run the existing entire suite with normal success criteria, no reruns.
    raise SystemExit(pytest.main(['-q', '-s', '--tb=short', 'app/tests', 'tests'],
                                plugins=[Plugin(args.output)]))
