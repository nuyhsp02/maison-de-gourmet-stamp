"""V1.4 deployment operations. Application/API/schema remain in ../app unchanged."""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, quote
from urllib.request import Request, urlopen


class OpsError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_source(directory):
    directory = Path(directory).resolve()
    name = '_maison_' + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, directory / 'app.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def settings(root, ready=True):
    root = Path(root).resolve()
    data = read_json(root / 'config/operations.json')
    for key, default in [('Port', 8765), ('MetricsPort', 20241), ('MinimumFreeMB', 256)]:
        value = data.get(key, default)
        if type(value) is not int or value < (1024 if key != 'MinimumFreeMB' else 16) or value > 65535 and key != 'MinimumFreeMB':
            raise OpsError('Invalid setting: ' + key)
        data[key] = value
    data.setdefault('ExternalBackupPaths', [])
    data.setdefault('DailyBackupTime', '03:00')
    if not isinstance(data['DailyBackupTime'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', data['DailyBackupTime']):
        raise OpsError('DailyBackupTime must be HH:mm in Windows local time')
    if data['Port'] == data['MetricsPort']:
        raise OpsError('App and metrics ports must differ')
    if not isinstance(data.get('ExternalBackupPaths', []), list) or not all(isinstance(p, str) and p for p in data.get('ExternalBackupPaths', [])):
        raise OpsError('ExternalBackupPaths must be an array of paths')
    if ready:
        url = data.get('PublicUrl', '')
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed.hostname in ('localhost', '127.0.0.1')
                or parsed.port not in (None, 443) or parsed.netloc != parsed.netloc.lower() or parsed.port == 443):
            raise OpsError('Set a canonical HTTPS domain without path, credentials or port')
        if any(ord(c) <= 32 for c in url):
            raise OpsError('Invalid public URL')
    return data


@contextlib.contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if path.stat().st_size == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OpsError('Another operation already holds the lock') from error
        yield
    finally:
        stream.close()  # Closing releases both Windows and POSIX locks; leave the file in place.


def readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise OpsError('Database does not exist')
    # Windows UNC needs an empty URI authority: file:////server/share/file.
    uri = 'file://' + quote(path.as_posix(), safe='/:') if path.as_posix().startswith('//') else path.as_uri()
    connection = sqlite3.connect(uri + '?mode=ro', uri=True, timeout=15)
    connection.execute('PRAGMA busy_timeout=15000')
    return connection


def inspect_database(path, event=None):
    with contextlib.closing(readonly(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise OpsError('Database integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise OpsError('Database foreign-key check failed')
        if event is not None:
            expected = json.dumps([event['id'], sorted(p['id'] for p in event['products'])])
            row = db.execute("SELECT value FROM metadata WHERE key='event'").fetchone()
            if not row or row[0] != expected:
                raise OpsError('Database belongs to a different event; initialization refused')
        return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('customers', 'stamps', 'reward_audit')}


def ensure_space(directory, source_size, minimum_mb):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    required = max(minimum_mb * 1024 * 1024, source_size * 3 + 16 * 1024 * 1024)
    if shutil.disk_usage(directory).free < required:
        raise OpsError('Insufficient free space; no backup removed')


def snapshot(source, target, minimum_mb=256, event=None):
    """SQLite backup API, new target, bounded busy wait, integrity then atomic publish."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or target.exists():
        raise OpsError('Snapshot target must be a new file')
    if not source.is_file():
        raise OpsError('Source database does not exist')
    ensure_space(target.parent, source.stat().st_size, minimum_mb)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
    started = time.monotonic()
    def progress(_status, _remaining, _total):
        if time.monotonic() - started > 120:
            raise OpsError('Database backup timed out')
    try:
        with contextlib.closing(readonly(source)) as src, contextlib.closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst, pages=128, progress=progress, sleep=.05)
            dst.commit()
            dst.execute('PRAGMA journal_mode=DELETE')
        inspect_database(temporary, event)
        with temporary.open('r+b') as handle:
            os.fsync(handle.fileno())
        if target.exists():
            raise OpsError('Snapshot target already exists')
        os.replace(temporary, target)
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = Path(str(temporary) + suffix)
            if candidate.exists():
                candidate.unlink()
    return target


def event_for(root, app_dir=None):
    module = load_source(app_dir or Path(root) / 'app')
    event = module.load_event(Path(root) / 'config/event.json')
    if event['draft']:
        raise OpsError('Event is still a draft; production startup refused')
    return module, event


def validate(root, app_dir=None, initialize=False):
    root = Path(root).resolve()
    cfg = settings(root)
    module, event = event_for(root, app_dir)
    database = root / 'data/collection.sqlite'
    if database.exists():
        counts = inspect_database(database, event)  # No init_db or write on an existing DB.
    elif initialize:
        module.init_db(database, event)
        counts = inspect_database(database, event)
    else:
        raise OpsError('Database missing; run the installer initialization step')
    return {'ready': True, 'public_url': cfg['PublicUrl'], 'database': str(database), 'counts': counts}


def prune(base, now):
    base = Path(base)
    cutoff = (now.date() - timedelta(days=13))
    week = now.date() - timedelta(days=now.weekday())
    for path in (base / 'daily').glob('maison-*.sqlite'):
        try:
            day = datetime.strptime(path.name[7:15], '%Y%m%d').date()
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()
    for path in (base / 'weekly').glob('maison-week-*.sqlite'):
        match = __import__('re').match(r'maison-week-(\d{4})-(\d{2})-', path.name)
        if not match:
            continue
        try:
            monday = datetime.fromisocalendar(int(match[1]), int(match[2]), 1).date()
        except ValueError:
            continue
        if monday < week - timedelta(weeks=3):
            path.unlink()


def backup(root, now=None, blocked_external=()):
    root = Path(root).resolve()
    cfg = settings(root, ready=False)
    module, event = event_for(root)
    now = now or datetime.now().astimezone()
    source, base = root / 'data/collection.sqlite', root / 'backups'
    with file_lock(root / 'data/backup.lock'):
        inspect_database(source, event)
        name = 'maison-' + now.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.sqlite'
        daily = snapshot(source, base / 'daily' / name, cfg['MinimumFreeMB'], event)
        iso = now.isocalendar()
        prefix = f'maison-week-{iso[0]}-{iso[1]:02}-'
        existing = sorted((base / 'weekly').glob(prefix + '*.sqlite'))
        if not existing:
            weekly = snapshot(daily, base / 'weekly' / (prefix + name[7:]), cfg['MinimumFreeMB'], event)
        else:
            weekly = existing[0]
            inspect_database(weekly, event)
        failures = []
        for location in cfg.get('ExternalBackupPaths', []):
            try:
                if location in blocked_external or not Path(location).is_absolute():
                    raise OpsError('External path unavailable or ACL validation failed')
                external = Path(location) / 'MaisonCafe'
                if external.resolve().is_relative_to(root):
                    raise OpsError('External backup path cannot be inside the installation')
                snapshot(daily, external / 'daily' / daily.name, cfg['MinimumFreeMB'], event)
                ext_week = external / 'weekly' / weekly.name
                if not ext_week.exists():
                    snapshot(weekly, ext_week, cfg['MinimumFreeMB'], event)
                else:
                    inspect_database(ext_week, event)
                prune(external, now)
            except (OSError, sqlite3.Error, OpsError):
                failures.append(str(location))
        result = {'time': now.isoformat(), 'daily': str(daily), 'weekly': str(weekly),
                  'status': 'partial' if failures else 'ok', 'external_failed': failures,
                  'counts': inspect_database(daily, event)}
        if not failures:
            prune(base, now)
        write_json(base / 'latest.json', result)
        audit(root, 'backup', result['status'])
        if failures:
            raise OpsError('Local backup succeeded but external copy failed; see backups/latest.json')
        return result


def stage_restore(root, backup_file):
    root = Path(root).resolve()
    cfg = settings(root, ready=False)
    _, event = event_for(root)
    name = 'restore-' + datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.sqlite'
    destination = root / 'data/recovery-check' / name
    snapshot(backup_file, destination, cfg['MinimumFreeMB'], event)
    result = {'candidate': str(destination), 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
              'counts': inspect_database(destination, event), 'source': str(Path(backup_file).resolve())}
    write_json(destination.with_suffix('.receipt.json'), result)
    audit(root, 'restore-stage', 'ok')
    return result


def import_database(root, backup_file):
    root = Path(root).resolve()
    cfg = settings(root)
    _, event = event_for(root)
    with file_lock(root / 'data/backup.lock'), file_lock(root / 'data/server.lock'):
        target = snapshot(backup_file, root / 'data/collection.sqlite', cfg['MinimumFreeMB'], event)
    audit(root, 'import-database', 'ok')
    return {'database': str(target), 'counts': inspect_database(target, event)}


def activate_restore(root, candidate, confirmed=False):
    root, candidate = Path(root).resolve(), Path(candidate).resolve()
    if not confirmed or not (root / 'config/maintenance.flag').exists():
        raise OpsError('Stop server and explicitly confirm restore first')
    if candidate.parent != root / 'data/recovery-check':
        raise OpsError('Use a validated recovery-check candidate')
    receipt = read_json(candidate.with_suffix('.receipt.json'))
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != receipt['sha256']:
        raise OpsError('Restore candidate changed; validate the backup again')
    cfg = settings(root, ready=False)
    _, event = event_for(root)
    database = root / 'data/collection.sqlite'
    with file_lock(root / 'data/server.lock'), file_lock(root / 'data/backup.lock'):
        inspect_database(candidate, event)
        snapshot(database, root / 'backups/pre-restore' / ('before-' + uuid.uuid4().hex + '.sqlite'), cfg['MinimumFreeMB'], event)
        ready = snapshot(candidate, root / 'data' / ('activate-' + uuid.uuid4().hex + '.sqlite'), cfg['MinimumFreeMB'], event)
        retired = root / 'data/retired' / (datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
        retired.mkdir(parents=True)
        moved = []
        try:
            for suffix in ('', '-wal', '-shm', '-journal'):
                path = Path(str(database) + suffix)
                if path.exists():
                    target = retired / path.name
                    path.rename(target)
                    moved.append((target, path))
            ready.rename(database)
        except Exception:
            for old, original in reversed(moved):
                if not original.exists():
                    old.rename(original)
            raise
        finally:
            if ready.exists(): ready.unlink()
        audit(root, 'restore-activate', 'ok')
        return {'database': str(database), 'retired': str(retired), 'counts': inspect_database(database, event)}


def audit(root, action, outcome):
    folder = Path(root) / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('operations-' + datetime.now().strftime('%Y-%m-%d') + '.jsonl')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': datetime.now().astimezone().isoformat(), 'action': action, 'outcome': outcome}) + '\n')


def request_health(cfg, port=None):
    request = Request('http://127.0.0.1:' + str(port or cfg['Port']) + '/health',
                      headers={'Host': urlsplit(cfg['PublicUrl']).netloc})
    with urlopen(request, timeout=3) as response:
        return response.status == 200 and json.load(response).get('ok') is True


def create_http(root, app_dir=None, database=None, port=None):
    from waitress import create_server
    root = Path(root).resolve()
    cfg = settings(root)
    module, _event = event_for(root, app_dir)
    app = module.create_app(root / 'config/event.json', database or root / 'data/collection.sqlite', cfg['PublicUrl'])
    sockets = {}
    server = create_server(app, host='127.0.0.1', port=cfg['Port'] if port is None else port,
                           threads=32, map=sockets, trusted_proxy='127.0.0.1', trusted_proxy_count=1,
                           trusted_proxy_headers={'x-forwarded-for', 'x-forwarded-proto'},
                           clear_untrusted_proxy_headers=True, max_request_body_size=4096,
                           channel_timeout=30, connection_limit=256, expose_tracebacks=False)
    return server, sockets, cfg


def close_http(server, sockets):
    from waitress import wasyncore
    server.accepting = False
    server.task_dispatcher.shutdown(timeout=20)
    wasyncore.close_all(sockets)


def run_http(server, sockets, should_stop):
    # Keep select/poll and socket shutdown on one thread. Cross-thread close_all
    # can raise EBADF in a pending select (observed in the integration test).
    while sockets and not should_stop():
        server.asyncore.loop(timeout=.5, map=sockets, use_poll=server.adj.asyncore_use_poll, count=1)


def selftest(root, app_dir=None):
    root = Path(root).resolve()
    validate(root, app_dir)
    cfg = settings(root)
    _module, event = event_for(root, app_dir)
    folder = root / 'data/validation'
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='check-', dir=folder) as temp:
        clone = snapshot(root / 'data/collection.sqlite', Path(temp) / 'check.sqlite', cfg['MinimumFreeMB'], event)
        server, sockets, cfg = create_http(root, app_dir, clone, port=0)
        stop = threading.Event()
        thread = threading.Thread(target=run_http, args=(server, sockets, stop.is_set), daemon=True)
        thread.start()
        try:
            if not request_health(cfg, int(server.effective_port)):
                raise OpsError('Local Waitress self-test failed')
        finally:
            stop.set()
            thread.join(10)
            if thread.is_alive():
                raise OpsError('Self-test server did not stop')
            close_http(server, sockets)
    return {'selftest': 'ok', 'production_database_used_for_http': False}


def setup_logging(root):
    folder = Path(root) / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    for name, level in [('server.log', logging.INFO), ('errors.log', logging.ERROR)]:
        handler = logging.handlers.RotatingFileHandler(folder / name, maxBytes=5 * 1024 * 1024,
                                                       backupCount=5, encoding='utf-8')
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        logger.addHandler(handler)


def serve(root):
    root = Path(root).resolve()
    if (root / 'config/maintenance.flag').exists():
        raise OpsError('Maintenance mode is active')
    validate(root)
    setup_logging(root)
    with file_lock(root / 'data/server.lock'):
        server, sockets, cfg = create_http(root)
        state = root / 'config/server.state.json'
        write_json(state, {'pid': os.getpid(), 'started': datetime.now().astimezone().isoformat(),
                           'python': sys.executable, 'port': cfg['Port']})
        logging.info('Waitress started on loopback, production mode')
        try:
            run_http(server, sockets, lambda: (root / 'config/stop.flag').exists()
                     or (root / 'config/maintenance.flag').exists())
        except KeyboardInterrupt:
            pass
        finally:
            close_http(server, sockets)
            state.unlink(missing_ok=True)
            logging.info('Waitress stopped')


def status(root):
    root = Path(root).resolve()
    cfg = settings(root, ready=False)
    result = {'maintenance': (root / 'config/maintenance.flag').exists(), 'local_healthy': False,
              'tunnel_ready': False, 'backup': None, 'last_backup_failure': None, 'server': None}
    if cfg.get('PublicUrl'):
        try: result['local_healthy'] = request_health(cfg)
        except (OSError, ValueError): pass
    try:
        with urlopen('http://127.0.0.1:' + str(cfg['MetricsPort']) + '/ready', timeout=2) as response:
            result['tunnel_ready'] = response.status == 200
    except OSError: pass
    for key, path in [('backup', root / 'backups/latest.json'), ('last_backup_failure', root / 'backups/last-failure.json'),
                      ('server', root / 'config/server.state.json')]:
        if path.is_file(): result[key] = read_json(path)
    result['free_bytes'] = shutil.disk_usage(root).free
    result['tunnel_log_bytes'] = (root / 'logs/tunnel.log').stat().st_size if (root / 'logs/tunnel.log').exists() else 0
    return result


def check_runtime(root):
    from importlib.metadata import version
    for line in (Path(root) / 'requirements-windows.lock').read_text().splitlines():
        if not line.strip() or line.startswith('#'): continue
        name, expected = line.strip().split('==')
        if version(name) != expected:
            raise OpsError('Runtime dependency mismatch: ' + name)
    return {'runtime_dependencies': 'ok'}


def check_package(package):
    package = Path(package).resolve()
    manifest = read_json(package / 'PACKAGE_MANIFEST.json')
    if manifest.get('format') != 'maison-windows-v1':
        raise OpsError('Unsupported package format')
    for relative, expected in manifest['sha256'].items():
        original = package / relative
        path = original.resolve()
        if not path.is_relative_to(package) or not path.is_file() or original.is_symlink():
            raise OpsError('Invalid package file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise OpsError('Package checksum mismatch: ' + relative)
    return {'package': 'ok', 'version': manifest['version']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--app-dir')
    parser.add_argument('action', choices=['validate', 'initialize', 'selftest', 'serve', 'backup', 'restore-stage',
                                          'restore-activate', 'import-database', 'status', 'check-package', 'check-runtime'])
    parser.add_argument('--file')
    parser.add_argument('--confirm-restore', action='store_true')
    parser.add_argument('--skip-external', action='append', default=[])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        functions = {'validate': lambda: validate(args.root, args.app_dir),
                     'initialize': lambda: validate(args.root, args.app_dir, initialize=True),
                     'selftest': lambda: selftest(args.root, args.app_dir), 'serve': lambda: serve(args.root),
                     'backup': lambda: backup(args.root, blocked_external=args.skip_external), 'restore-stage': lambda: stage_restore(args.root, args.file),
                     'import-database': lambda: import_database(args.root, args.file),
                     'check-runtime': lambda: check_runtime(args.root),
                     'restore-activate': lambda: activate_restore(args.root, args.file, args.confirm_restore),
                     'status': lambda: status(args.root), 'check-package': lambda: check_package(args.file)}
        result = functions[args.action]()
        if result is not None: print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        # No request bodies, environment, secret files or full command line are logged.
        try:
            audit(args.root, args.action, 'failed:' + type(error).__name__)
            if args.action == 'backup':
                write_json(Path(args.root) / 'backups/last-failure.json',
                           {'time': datetime.now().astimezone().isoformat(), 'failure_type': type(error).__name__})
        except OSError:
            pass  # Disk full/unwritable logs must still return a clear nonzero exit.
        message = str(error) if isinstance(error, OpsError) else type(error).__name__
        print('Operation failed: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
