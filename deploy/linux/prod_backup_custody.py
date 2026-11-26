"""Inventory and expiry of PROD copies on explicitly enrolled POSIX storage."""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat

import prod

KINDS = {'local', 'external', 'archive', 'restore', 'snapshot', 'temporary', 'previous', 'quarantine'}
MARKER = '.maison-custody-volume'


def location_id(volume, name):
    # The first half identifies a verified volume, even after a transient file disappears.
    return volume + hashlib.sha256(name.encode()).hexdigest()[:32]


class Roots:
    def __init__(self, data, event_id, instance_id):
        if (data.get('format') != 'prod-backup-roots-v1' or data.get('event_id') != event_id
                or data.get('instance_id') != instance_id or not isinstance(data.get('volumes'), list)):
            raise ValueError('Custody plan is not bound to this PROD instance')
        self.instance = instance_id
        self.volumes = {}
        ids = set()
        for row in data['volumes']:
            root, volume = Path(row['path']), row['volume_id']
            kinds = set(row['kinds'])
            if (not root.is_absolute() or root.resolve() != root or not re.fullmatch('[0-9a-f]{32}', volume)
                    or not kinds or not kinds <= KINDS or type(row['service']) is not bool or volume in ids
                    or any(root.is_relative_to(other) or other.is_relative_to(root) for other in self.volumes)
                    or (row['service'] and kinds != {'temporary'})):
                raise ValueError('Invalid or overlapping custody roots')
            self.volumes[root] = row
            ids.add(volume)
        self.manual = set(data['manual_kinds'])
        if not self.manual <= KINDS:
            raise ValueError('Invalid independently managed custody classes')

    @classmethod
    def from_env(cls, event_id, instance):
        path = prod.canonical(os.environ.get('MAISON_PROD_BACKUP_ROOTS', ''))
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != os.environ.get('MAISON_PROD_BACKUP_ROOTS_SHA256'):
            raise ValueError('Custody roots lack their independently configured fingerprint')
        return cls(json.loads(content), event_id, instance)

    def verify_volume(self, root):
        row = self.volumes[root]
        prod.private(root, directory=True)
        if row['service']:
            # The live instance is excluded only on the separately assigned service volume.
            from contextlib import closing
            dbpath = prod.database_path(root)
            prod.check_files(dbpath)
            with closing(prod.application.connect_db(dbpath, mode='ro')) as db:
                if dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('prod_instance_id','linux_prod_v1')")) != {
                        'prod_instance_id': self.instance, 'linux_prod_v1': '1'}:
                    raise ValueError('Wrong live instance on custody root')
        elif prod.private(root / MARKER).read_text().strip() != row['volume_id']:
            raise ValueError('Custody volume unavailable or substituted')

    @contextmanager
    def lock(self, roots, *, exclusive=False):
        import fcntl
        with ExitStack() as stack:
            for root in sorted(set(roots)):
                if root not in self.volumes:
                    raise ValueError('Artifact is outside the enrolled custody roots')
                prod.private(root, directory=True)
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                self.verify_volume(root)
            yield

    def ident(self, path, kind):
        row = self.volumes.get(path.parent)
        if (row is None or kind not in row['kinds'] or path.name == MARKER or not path.name
                or (row['service'] and path.name == 'instance')):
            raise ValueError('Unexpected artifact location or custody class')
        return location_id(row['volume_id'], path.name)


def inspect_entry(path, device):
    info = path.lstat()
    if (info.st_dev != device or info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode)
            or (not stat.S_ISDIR(info.st_mode) and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1))
            or (stat.S_ISDIR(info.st_mode) and os.path.ismount(path))):
        raise ValueError('Unexpected owner, mount, link or special file in expiry target')
    if stat.S_ISDIR(info.st_mode):
        for item in path.iterdir():
            inspect_entry(item, device)


def observe(plan, records, ledger, binding):
    found = {}
    for root, volume in plan.volumes.items():
        for item in root.iterdir():
            if item.name == ('instance' if volume['service'] else MARKER):
                continue
            if volume['service'] and item.name == '.privacy-closed':
                prod.private(item, directory=True)
                if any(item.iterdir()):
                    raise ValueError('Unexpected data in service closure latch')
                continue
            ident = location_id(volume['volume_id'], item.name)
            row = records.get(ident)
            if row is None or row['kind'] not in volume['kinds']:
                ledger.flag_unknown(*binding, ident)
                raise ValueError('Unregistered artifact. Reconcile custody before deletion')
            if row['deleted_at'] is not None:
                ledger.discover(*binding, row['kind'], ident)
                raise ValueError('An artifact reappeared after deletion. Inspect its custody history')
            inspect_entry(item, root.stat().st_dev)
            found[ident] = item
    return found


def receipt(folder, proof):
    payload = (json.dumps(proof, sort_keys=True, separators=(',', ':')) + '\n').encode()
    digest = hashlib.sha256(payload).hexdigest()
    with open(folder / (secrets.token_hex(16) + '.json'), 'xb',
              opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())
    prod.sync_dir(folder)
    return digest


def expire(plan, ledger, event_id, instance, cutoff, proof_dir):
    """Run at the custodian, with all plan volumes available and independent receipt storage."""
    proof_dir = prod.private(proof_dir, directory=True)
    prod.separate_destination(proof_dir)
    if any(proof_dir.is_relative_to(root) or root.is_relative_to(proof_dir) for root in plan.volumes):
        raise ValueError('Expiry receipts must be outside every custody volume')
    volume_kinds = {row['volume_id']: set(row['kinds']) for row in plan.volumes.values()}

    def covered(ident, row):
        return row['kind'] in volume_kinds.get(ident[:32], ())
    ctx = (event_id, instance, cutoff)
    with plan.lock(plan.volumes, exclusive=True):
        with ledger.transaction(*ctx) as (db, current):
            status = ledger._status(db)
        if status.closed:
            if current < cutoff or not status.purge_authorized:
                raise ValueError('The custody deadline and approved disposition are required')
        elif not status.backup_ready:
            raise ValueError('Approved daily retention and inventory are required')
        records = {row['object_id']: row for row in ledger.objects(*ctx)}
        # Unmounted provider-native object IDs are never inferred absent; their custodian
        # must provide a provider-specific deletion receipt and full source reconciliation.
        if any(not covered(ident, row) and row['kind'] not in plan.manual and row['deleted_at'] is None
               for ident, row in records.items()):
            raise ValueError('Inventory contains a copy on an unverified volume')
        found = observe(plan, records, ledger, ctx)
        due = {ident: row for ident, row in records.items() if row['deleted_at'] is None
               and covered(ident, row) and (status.closed or current >= min(cutoff,
                                                    row['created_at'] + status.retention_days * 86400))}
        for ident in due:
            path = found.get(ident)
            if path is not None:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                prod.sync_dir(path.parent)
        remaining = observe(plan, records, ledger, ctx)
        if remaining.keys() & due.keys():
            raise ValueError('Custody enumeration still contains an expired artifact')
        header = {'format': 'prod-posix-expiry-v1', 'event': event_id, 'instance': instance,
                  'verified_at': current, 'cutoff': cutoff}
        for ident in due:
            proof = receipt(proof_dir, dict(header, object_id=ident, kind=due[ident]['kind'],
                                           observed_before=ident in found, observed_after=False))
            ledger.attest_deleted(*ctx, ident, proof)
        reconciled = []
        if status.closed:
            for kind in sorted(KINDS - plan.manual):
                volumes = sorted(row['volume_id'] for row in plan.volumes.values() if kind in row['kinds'])
                if volumes and not any(records[ident]['kind'] == kind for ident in remaining):
                    proof = receipt(proof_dir, dict(header, kind=kind, checked_volumes=volumes, remaining=0))
                    ledger.reconcile(*ctx, kind, proof)
                    reconciled.append(kind)
        return {'verified_absent': len(due), 'reconciled': reconciled}


def main():
    from prod_local_lifecycle import local_ledger
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, help='Explicit local lifecycle path (must match service configuration)')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    args = parser.parse_args()
    try:
        event = prod.load_prod_event(args.config)
        plan = Roots.from_env(event['id'], args.instance)
        ledger = local_ledger()
        if args.ledger is not None and args.ledger != ledger.path:
            raise ValueError("Unbound inventory ledger")
        result = expire(plan, ledger, event['id'], args.instance,
                        prod.application.timestamp(event['access_until']), args.receipts)
    except (OSError, ValueError, KeyError, RuntimeError):
        parser.exit(1, 'Custody expiry incomplete. Check the ledger, inventory and volume evidence.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
