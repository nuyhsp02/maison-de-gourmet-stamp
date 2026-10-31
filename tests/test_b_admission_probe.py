"""Deterministic checks for diagnostic arithmetic, no overload simulation."""
from b_admission_probe import Probe, overlap, stats
import threading


def test_probe_summary_distinguishes_progress_from_one_long_holder():
    probe = Probe(threading.Lock())
    probe.now = lambda: 20
    probe.waits = [(0, 16, False)]
    probe.holds = [(i, i + .1) for i in range(16)]
    probe.phases = [('commit', i, i + .08, True) for i in range(16)]
    probe.responses = [(i + .2, 200) for i in range(16)]
    data = probe.summary()
    slow = data['slow_waits'][0]
    assert data['timeouts'] == 1
    assert slow['other_grants'] == 15
    assert slow['successful_write_responses'] == 16
    assert slow['held_wall_ms'] == 1600
    assert slow['commit_wall_ms'] == 1280
    assert overlap(0,1,2,3) == 0
    assert stats([.001,.002])['p99_ms'] == 2


def test_probe_keeps_native_timeout_and_unlock_behavior():
    native = threading.Lock()
    probe = Probe(native)
    assert probe.acquire(timeout=.1)
    assert not probe.acquire(timeout=.001)
    probe.release()
    assert not native.locked()
    assert probe.summary()['timeouts'] == 1
