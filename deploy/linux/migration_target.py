"""Private receipt for the freshly created databases used in a synthetic migration drill."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

DATABASE_NAME = "collection.sqlite"
LIVE_STATE = Path("/var/lib/maison-demo")
RECEIPT = "migration-target.json"
PURPOSES = ("contacts", "corrections", "contacts+corrections")
SCHEMAS = ("base", *PURPOSES)


class MigrationError(ValueError):
    """Stable, operator-visible reason. Never include a supplied path or database value."""


def canonical(path, *, new=False):
    if path is None:
        raise MigrationError("migration_confirmation_missing")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise MigrationError("migration_noncanonical_path")
    if path.is_relative_to(LIVE_STATE) or LIVE_STATE.is_relative_to(path):
        raise MigrationError("migration_live_path")
    existing = path.parent if new else path
    try:
        resolved = existing.resolve(strict=True)
    except (OSError, RuntimeError):
        raise MigrationError("migration_path_unavailable") from None
    if resolved != existing:
        raise MigrationError("migration_noncanonical_path")
    return path


def private_stat(path, *, directory=False):
    info = path.lstat()
    expected = 0o700 if directory else 0o600
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected:
        raise MigrationError("migration_unsafe_file")
    if not directory and info.st_nlink != 1:
        raise MigrationError("migration_path_alias")
    return [info.st_dev, info.st_ino]


def write_private(path, data):
    with open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        out.write(data)


@dataclass(frozen=True)
class Target:
    source: Path
    target: Path

    @property
    def receipt(self):
        return self.source.parent / RECEIPT

    def identities(self):
        result = {}
        for directory in (self.source, self.target):
            canonical(directory)
            private_stat(directory, directory=True)
            db = directory / DATABASE_NAME
            for suffix in ("", ".profile.key"):
                result[directory.name + "/" + DATABASE_NAME + suffix] = private_stat(Path(str(db) + suffix))
            # SQLite can open these files implicitly. Reject an existing alias before it does.
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    private_stat(Path(str(db) + suffix))
                except FileNotFoundError:
                    pass
        if (self.source / DATABASE_NAME).samefile(self.target / DATABASE_NAME):
            raise MigrationError("migration_same_database")
        return result

    def bind(self, event_id, inventory, purpose):
        proof = {"format": "synthetic-migration-v1", "source": str(self.source), "target": str(self.target),
                 "event_id": event_id, "purpose": purpose, "source_inventory": inventory, "files": self.identities()}
        write_private(self.receipt, json.dumps(proof).encode())

    @classmethod
    def confirmed(cls, source_dir, target_dir, event_id, schema, purpose):
        if not source_dir or not target_dir or not event_id or schema not in SCHEMAS:
            raise MigrationError("migration_confirmation_missing")
        source, target = canonical(source_dir), canonical(target_dir)
        if source == target:
            raise MigrationError("migration_same_database")
        if source.parent != target.parent or source.name != "source" or target.name != "target":
            raise MigrationError("migration_wrong_target")
        pair = cls(source, target)
        private_stat(source.parent, directory=True)
        try:
            private_stat(pair.receipt)
            proof = json.loads(pair.receipt.read_bytes())
        except MigrationError:
            raise
        except (FileNotFoundError, ValueError):
            raise MigrationError("migration_confirmation_missing") from None
        if (not isinstance(proof, dict) or proof.get("format") != "synthetic-migration-v1"
                or not isinstance(proof.get("source_inventory"), str)):
            raise MigrationError("migration_confirmation_missing")
        if proof.get("source") != str(source) or proof.get("target") != str(target):
            raise MigrationError("migration_wrong_target")
        approved = proof.get("purpose")
        if approved not in PURPOSES or purpose not in approved.split("+"):
            raise MigrationError("migration_purpose")
        if proof.get("event_id") != event_id:
            raise MigrationError("migration_event_id")
        if proof.get("files") != pair.identities():
            raise MigrationError("migration_file_changed")
        return pair, proof
