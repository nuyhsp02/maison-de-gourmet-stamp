"""Bounded, test-only timing/counter observations; never collect request data."""
import platform
import re
import sqlite3
import sys
import threading
import time
from collections import deque


class ObservedLock:
    def __init__(self, lock):
        self.lock = lock
        self.metrics_lock = threading.Lock()
        self.waiting = self.peak_waiting = 0
        self.granted = self.released = self.timeouts = 0
        self.max_wait_ms = self.max_hold_ms = 0.0
        self.max_grants_while_waiting = 0
        self.active_since = None
        self.open_transactions = 0
        self.timeout_samples = deque(maxlen=8)

    def acquire(self, blocking=True, timeout=-1):
        started = time.monotonic()
        with self.metrics_lock:
            self.waiting += 1
            self.peak_waiting = max(self.peak_waiting, self.waiting)
            earlier_grants = self.granted
        acquired = self.lock.acquire(blocking, timeout)
        now = time.monotonic()
        with self.metrics_lock:
            self.waiting -= 1
            waited = (now - started) * 1000
            bypasses = self.granted - earlier_grants
            self.max_wait_ms = max(self.max_wait_ms, waited)
            self.max_grants_while_waiting = max(self.max_grants_while_waiting, bypasses)
            if acquired:
                self.granted += 1
                self.active_since = now
            else:
                self.timeouts += 1
                self.timeout_samples.append({'wait_ms': round(waited, 2),
                                             'grants_while_waiting': bypasses})
        return acquired

    def release(self):
        with self.metrics_lock:
            if self.active_since is not None:
                self.max_hold_ms = max(self.max_hold_ms, (time.monotonic() - self.active_since) * 1000)
            self.active_since = None
            self.released += 1
        self.lock.release()

    def observe_transaction(self, active):
        if active:
            with self.metrics_lock:
                self.open_transactions += 1

    def summary(self, records):
        with self.metrics_lock:
            result = {'granted': self.granted, 'released': self.released,
                      'waiting': self.waiting, 'peak_waiting': self.peak_waiting,
                      'admission_timeouts': self.timeouts, 'max_wait_ms': round(self.max_wait_ms, 2),
                      'max_hold_ms': round(self.max_hold_ms, 2),
                      'max_grants_while_waiting': self.max_grants_while_waiting,
                      'open_transactions_before_close': self.open_transactions,
                      'timeout_samples': list(self.timeout_samples)}
        result.update(failure_counts(records))
        result['mode'] = 'timing_observer'
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
