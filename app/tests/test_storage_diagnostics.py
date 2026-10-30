import json
import sqlite3

from conftest import BASE, Visitor


def test_admission_timeout_is_distinct_from_sqlite_error(app, monkeypatch, caplog):
    visitor = Visitor(app)
    class Denied:
        def acquire(self, timeout):
            assert timeout == 15
            return False
        def release(self):
            raise AssertionError('Unowned lock must not be released')
    monkeypatch.setattr(app, 'write_lock', Denied())
    response = visitor.post('/api/collect', {'qr': 'synthetic'})
    assert response.status_code == 503 and response.json['code'] == 'storage_unavailable'
    assert any(r.msg == 'Write admission timed out' for r in caplog.records)
    assert not any(r.msg == 'Database operation failed: %s (%s)' for r in caplog.records)


def test_sqlite_error_classification_does_not_log_sensitive_message(app, monkeypatch, caplog):
    import app as module
    error = sqlite3.OperationalError('private-parameter-not-for-logs')
    error.sqlite_errorname = 'SQLITE_BUSY'
    error.sqlite_errorcode = 5
    def unavailable(_path):
        raise error
    monkeypatch.setattr(module, 'connect_db', unavailable)
    response = app.test_client().post('/api/session', json={}, base_url=BASE,
                                     headers={'Origin': BASE, 'X-Stamp-Client': '1'})
    assert response.status_code == 503 and response.json['code'] == 'storage_unavailable'
    assert any(r.msg == 'Database operation failed: %s (%s)' for r in caplog.records)
    assert not any(r.msg == 'Write admission timed out' for r in caplog.records)
    assert 'private-parameter-not-for-logs' not in caplog.text + json.dumps(response.json)
    assert app.write_lock.acquire(timeout=1)
    app.write_lock.release()


def test_real_admission_deadline_without_sqlite_failure(app, keys, caplog):
    """Deliberately hold the real slot: keep the production 15-second deadline."""
    import time
    visitor = Visitor(app)
    assert app.write_lock.acquire(timeout=1)
    started = time.monotonic()
    try:
        response = visitor.post('/api/collect', {'qr': keys[0]})
        assert response.status_code == 503 and response.json['code'] == 'storage_unavailable'
        assert time.monotonic() - started >= 14.9
        assert any(r.msg == 'Write admission timed out' for r in caplog.records)
        assert not any(r.msg == 'Database operation failed: %s (%s)' for r in caplog.records)
    finally:
        app.write_lock.release()
    assert visitor.collect(keys[0])['added'] is True


def test_real_sqlite_busy_without_admission_timeout(app, keys, caplog):
    """An external SQLite writer, not the Python slot, blocks BEGIN IMMEDIATE."""
    from contextlib import closing
    visitor = Visitor(app)
    with closing(sqlite3.connect(app.config['DATABASE'], isolation_level=None)) as external:
        external.execute('BEGIN IMMEDIATE')
        try:
            response = visitor.post('/api/collect', {'qr': keys[0]})
            assert response.status_code == 503 and response.json['code'] == 'storage_unavailable'
            assert any(r.msg == 'Database operation failed: %s (%s)' and r.args[1] == 'SQLITE_BUSY'
                       for r in caplog.records)
            assert not any(r.msg == 'Write admission timed out' for r in caplog.records)
        finally:
            external.rollback()
    assert app.write_lock.acquire(timeout=1)
    app.write_lock.release()
    assert visitor.collect(keys[0])['added'] is True
