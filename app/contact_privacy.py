"""Synthetic enrollment and operator-only sealed contact records."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from nacl.public import PublicKey, SealedBox

NOTICE = "synthetic-contact-v1"
ROOT = Path(__file__).resolve().parent
CONTACT_TABLES = {"customer_contacts", "contact_access_audit"}
SEPARATOR = r"(?: *- *| +)"
GROUPS = r"([0-9]{3,4})(?:%s)?([0-9]{4})" % SEPARATOR


def phone_digits(raw):
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    value = raw.strip(" ")
    international = value.startswith("+82")
    prefix = r"1[016789]" if international else r"01[016789]"
    if international:
        value = re.sub(r"^\+82(?: *- *| *)", "", value)
    match = re.fullmatch(rf"(?:({prefix})|\(({prefix})\))(?:{SEPARATOR})?{GROUPS}", value)
    if match:
        initial, bracketed, middle, end = match.groups()
        digits = ("0" if international else "") + (initial or bracketed) + middle + end
        if not digits.startswith("010") or len(digits) == 11:
            return digits
    return None


def masked(number):
    return number[:3] + "-" + "*" * (len(number) - 7) + "-" + number[-4:]


def profile_tag(name, number):
    return hashlib.sha256((name + "\0" + number).encode()).hexdigest()


@dataclass(frozen=True)
class SyntheticEnrollment:
    event_id: str
    public_key: PublicKey
    allowed_profiles: frozenset

    @property
    def key_id(self):
        return hashlib.sha256(bytes(self.public_key)).hexdigest()

    @classmethod
    def read(cls, path, event_id):
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
        if (obj.get("format") != NOTICE or obj.get("event_id") != event_id
                or not isinstance(obj.get("allowed_profiles"), list)
                or not all(isinstance(t, str) and re.fullmatch("[a-f0-9]{64}", t) for t in obj["allowed_profiles"])):
            raise ValueError("합성 가입 설정의 행사 또는 형식을 확인하세요.")
        return cls(event_id, PublicKey(bytes.fromhex(obj["public_key"])), frozenset(obj["allowed_profiles"]))

    def store(self, db, cid, number, now):
        if contact_key_id(db) != self.key_id:
            raise ValueError("연락처 DB와 수신 키가 일치하지 않습니다.")
        data = json.dumps([NOTICE, self.event_id, cid, number], separators=(",", ":")).encode()
        sealed = SealedBox(self.public_key).encrypt(data)
        db.execute("INSERT INTO customer_contacts VALUES (?, ?, ?, 1, ?, ?)",
                   (cid, sealed, self.key_id, masked(number), now))


def schema_rows(db):
    return [tuple(row) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]


def contact_key_id(db):
    marker = db.execute("SELECT value FROM metadata WHERE key='contact_recipient_v1'").fetchone()
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('customer_contacts','contact_access_audit')")}
    if not tables and marker is None:
        return None
    if not marker or not re.fullmatch(r"[a-f0-9]{64}", marker[0]) or tables != CONTACT_TABLES:
        raise ValueError("연락처 스키마·수신 키 식별 정보가 불완전합니다.")
    return marker[0]


def apply_contact_schema(db, key_id):
    """Caller holds BEGIN IMMEDIATE. Accept only the published source/target DDL."""
    if not db.in_transaction or not re.fullmatch("[a-f0-9]{64}", key_id):
        raise ValueError("명시적 이관 트랜잭션과 공개키가 필요합니다.")
    old = contact_key_id(db)
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        before = schema_rows(reference)
        ddl = (ROOT / "contact_schema.sql").read_text(encoding="utf-8")
        reference.executescript(ddl)
        after = schema_rows(reference)
    if old:
        if old != key_id or schema_rows(db) != after:
            raise ValueError("연락처 키 또는 이관된 스키마가 일치하지 않습니다.")
        return
    if schema_rows(db) != before:
        raise ValueError("기준 스키마와 일치하지 않아 이관을 중단했습니다.")
    statement = ""
    for line in ddl.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip() or schema_rows(db) != after:
        raise ValueError("연락처 스키마 검사가 실패했습니다.")
    db.execute("INSERT INTO metadata VALUES ('contact_recipient_v1', ?)", (key_id,))
