"""Explicit additive correction audit migration for previously initialized cards."""
from contextlib import closing
from functools import lru_cache
import sqlite3

if __package__:
    from . import contact_privacy as contact
else:
    import contact_privacy as contact


def audit_objects(db):
    return {tuple(row) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='stamp_correction_audit' "
        "AND name NOT GLOB 'sqlite_*'")}


@lru_cache(maxsize=1)
def expected_objects():
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript((contact.ROOT / "correction_schema.sql").read_text(encoding="utf-8"))
        return frozenset(audit_objects(reference))


def correction_enabled(db):
    marker = db.execute("SELECT value FROM metadata WHERE key='correction_schema_v1'").fetchone()
    objects = audit_objects(db)
    if marker is None and not objects:
        return False
    if marker is None or marker[0] != "1" or objects != expected_objects():
        raise ValueError("관리자 정정 스키마 또는 이관 기록이 불완전합니다.")
    return True


def apply_correction_schema(db):
    """The caller holds BEGIN IMMEDIATE and rolls back on failure."""
    installed = correction_enabled(db)
    companion = "contact_schema.sql" if contact.contact_key_id(db) else None
    contact.apply_extension(db, "correction_schema.sql", installed, companion)
    if not installed:
        db.execute("INSERT INTO metadata VALUES ('correction_schema_v1', '1')")
