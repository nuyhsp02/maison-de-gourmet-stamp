"""Inspect the interpreter's actual SQLite, without opening customer data."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing


def inspect_runtime():
    with closing(sqlite3.connect(':memory:')) as db:
        source = db.execute('SELECT sqlite_source_id()').fetchone()[0]
    version = sqlite3.sqlite_version_info
    fixed = version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)
    return {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version,
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
