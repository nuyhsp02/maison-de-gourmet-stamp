"""Test-only observations, without adding a second contended lock."""
import json
import platform
import re
import sqlite3
import sys
import time
from collections import deque


class ObservedLock:
    def __init__(self, lock):
        self.lock = lock
        # deque.append is thread-safe; no metrics mutex changes waiter scheduling.
        # These collections are bounded by the fixed 5200-request test workload.
        self.events = deque()
        self.grants = deque()
        self.releases = deque()
        self.waits = deque()
        self.holds = deque()
        self.open_transactions = deque()
        self.timeout_samples = deque(maxlen=8)
        self.stream_slots = deque(range(8))
        self.active_since = None

    def acquire(self, blocking=True, timeout=-1):
        started = time.monotonic()
        earlier_grants = len(self.grants)
        self.events.append(1)
        acquired = self.lock.acquire(blocking, timeout)
        now = time.monotonic()
        bypasses = len(self.grants) - earlier_grants
        self.events.append(-1)
        self.waits.append((now - started, bypasses, acquired))
        if acquired:
            self.grants.append(now)
            self.active_since = now
        else:
            held_since = self.active_since
            sample = {'wait_ms': round((now - started) * 1000, 2),
                                         'grants_while_waiting': bypasses,
                                         'current_holder_age_ms': None if held_since is None else
                                         round((now - held_since) * 1000, 2)}
            self.timeout_samples.append(sample)
            try:
                self.stream_slots.popleft()
            except IndexError:
                pass
            else:
                # Survives an outer CI job timeout; at most eight numeric samples.
                print('STORAGE_TIMEOUT_SAMPLE ' + json.dumps(sample), file=sys.__stdout__, flush=True)
        return acquired

    def release(self):
        now = time.monotonic()
        if self.active_since is not None:
            self.holds.append(now - self.active_since)
        self.active_since = None
        self.releases.append(now)
        self.lock.release()

    def observe_transaction(self, active):
        if active:
            self.open_transactions.append(True)

    def summary(self, records):
        waiting = peak = 0
        for delta in self.events:
            waiting += delta
            peak = max(peak, waiting)
        result = {'granted': len(self.grants), 'released': len(self.releases),
                  'waiting': waiting, 'peak_waiting': peak,
                  'admission_timeouts': sum(not row[2] for row in self.waits),
                  'max_wait_ms': round(max((row[0] for row in self.waits), default=0) * 1000, 2),
                  'max_hold_ms': round(max(self.holds, default=0) * 1000, 2),
                  'max_grants_while_waiting': max((row[1] for row in self.waits), default=0),
                  'open_transactions_before_close': len(self.open_transactions),
                  'timeout_samples': list(self.timeout_samples)}
        result.update(failure_counts(records))
        result['mode'] = 'timing_observer_no_metrics_mutex'
        result['runtime'] = runtime_info()
        return result


def runtime_info():
    return {'os': platform.system(), 'python': sys.version.split()[0],
            'sqlite': sqlite3.sqlite_version, 'sqlite_threadsafety': sqlite3.threadsafety}


def failure_counts(records):
    labels = {}
    for record in records:
        if record.msg == 'Database operation failed: %s (%s)':
            name = record.args[1]
            name = name if isinstance(name, str) and re.fullmatch(r'SQLITE_[A-Z_]+', name) else 'unknown'
            labels[name] = labels.get(name, 0) + 1
    return {'sqlite_operational_errors': sum(labels.values()), 'sqlite_error_names': labels,
            'admission_timeout_logs': sum(r.msg == 'Write admission timed out' for r in records)}
