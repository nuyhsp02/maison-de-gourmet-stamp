"""Inspect the interpreter's actual SQLite, without opening customer data."""
import argparse
import json
import sqlite3
import sys
import struct
import sysconfig
from contextlib import closing


def inspect_runtime():
    with closing(sqlite3.connect(':memory:')) as db:
        source = db.execute('SELECT sqlite_source_id()').fetchone()[0]
    version = sqlite3.sqlite_version_info
    fixed = version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)
    return {'python': sys.version.split()[0], 'sys_version': sys.version,
            'implementation': sys.implementation.name, 'bits': struct.calcsize('P') * 8,
            'free_threaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
            'gil_enabled': getattr(sys, '_is_gil_enabled', lambda: True)(), 'sqlite': sqlite3.sqlite_version,
            'sqlite_source_id': source, 'official_fixed_version_range': fixed,
            'vendor_backport_verified': False,
            'reference': 'https://sqlite.org/wal.html#walreset'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-fixed', action='store_true')
    args = parser.parse_args()
    result = inspect_runtime()
    print(json.dumps(result))
    if args.require_fixed and not result['official_fixed_version_range']:
        raise SystemExit(1)
