"""Run the unchanged workload again without timing/locking observation."""
import threading

from test_load import test_400_visitors_30_concurrent as run_original_workload


def test_native_400_visitors_30_concurrent(app, keys):
    assert type(app.write_lock) is type(threading.Lock())
    run_original_workload(app, keys)
