"""Environment-bound enrollment and operator-only sealed contact records."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat

from nacl.bindings import crypto_scalarmult
from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox

NOTICE = "synthetic-contact-v1"
PROD_CONTACT = "prod-contact-v1"
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
        store_contact(self, NOTICE, db, cid, number, now)


def store_contact(enrollment, envelope_format, db, cid, number, now):
    if contact_key_id(db) != enrollment.key_id:
        raise ValueError("연락처 DB와 수신 키가 일치하지 않습니다.")
    data = json.dumps([envelope_format, enrollment.event_id, cid, number], separators=(",", ":")).encode()
    sealed = SealedBox(enrollment.public_key).encrypt(data)
    db.execute("INSERT INTO customer_contacts VALUES (?, ?, ?, 1, ?, ?)",
               (cid, sealed, enrollment.key_id, masked(number), now))


@dataclass(frozen=True)
class ProductionEnrollment:
    event_id: str
    public_key: PublicKey
    policy_path: Path | None
    approved_policy_sha256: str | None

    @property
    def key_id(self):
        return hashlib.sha256(bytes(self.public_key)).hexdigest()

    @classmethod
    def read(cls, path, event_id, policy_path=None, *, approved_policy_sha256=None):
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(obj, dict) or set(obj) != {"format", "event_id", "public_key"}
                or obj["format"] != "prod-contact-recipient-v1" or obj["event_id"] != event_id
                or not isinstance(obj["public_key"], str) or not re.fullmatch("[a-f0-9]{64}", obj["public_key"])):
            raise ValueError("PROD 행사와 전용 연락처 수신 공개키를 확인하세요.")
        public_key = PublicKey(bytes.fromhex(obj["public_key"]))
        # libsodium rejects small-order recipients. This public probe scalar is never used to seal a contact.
        try:
            crypto_scalarmult(bytes([1]) * 32, bytes(public_key))
        except CryptoError:
            raise ValueError("PROD 연락처 수신 공개키가 올바르지 않습니다.") from None
        return cls(event_id, public_key, Path(policy_path) if policy_path is not None else None,
                   approved_policy_sha256)

    def approved_notice(self):
        """Only the explicitly pinned, complete approval can open enrollment. Withdrawal takes effect live."""
        if not self.policy_path or not re.fullmatch("[a-f0-9]{64}", self.approved_policy_sha256 or ""):
            return None
        try:
            if not stat.S_ISREG(self.policy_path.lstat().st_mode):
                return None
            with self.policy_path.open("rb") as stream:
                raw = stream.read(32769)
            if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != self.approved_policy_sha256:
                return None
            policy = json.loads(raw)
            fields = {"format", "status", "event_id", "recipient_key_id", "notice_version", "notice_text",
                      "purpose", "legal_basis_ref", "retention", "inquiry_contact", "approved_at"}
            if (not isinstance(policy, dict) or set(policy) != fields
                    or any(not isinstance(v, str) or not v.strip() for v in policy.values())
                    or policy["format"] != "prod-registration-policy-v1" or policy["status"] != "approved"
                    or policy["event_id"] != self.event_id or policy["recipient_key_id"] != self.key_id
                    or not re.fullmatch(r"prod-[a-z0-9][a-z0-9.-]{0,58}", policy["notice_version"])):
                return None
            approved_at = datetime.fromisoformat(policy["approved_at"].replace("Z", "+00:00"))
            if approved_at.tzinfo is None or approved_at > datetime.now(timezone.utc):
                return None
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        return policy

    def store(self, db, cid, number, now):
        store_contact(self, PROD_CONTACT, db, cid, number, now)


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


def apply_extension(db, filename, installed, companion):
    """Only extend the exact published source schema, inside the caller's write transaction."""
    if not db.in_transaction:
        raise ValueError("명시적 이관 트랜잭션이 필요합니다.")
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        if companion:
            reference.executescript((ROOT / companion).read_text(encoding="utf-8"))
        before = schema_rows(reference)
        ddl = (ROOT / filename).read_text(encoding="utf-8")
        reference.executescript(ddl)
        after = schema_rows(reference)
    if schema_rows(db) != (after if installed else before):
        raise ValueError("기준 스키마와 일치하지 않아 이관을 중단했습니다.")
    if installed:
        return
    # executescript() would commit the caller's transaction before applying DDL.
    statement = ""
    for line in ddl.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip() or schema_rows(db) != after:
        raise ValueError("이관 후 스키마 검사가 실패했습니다.")


def apply_contact_schema(db, key_id):
    if not re.fullmatch("[a-f0-9]{64}", key_id):
        raise ValueError("수신 공개키 식별 정보가 필요합니다.")
    old = contact_key_id(db)
    if old is not None and old != key_id:
        raise ValueError("연락처 수신 키 식별 정보가 일치하지 않습니다.")
    correction = db.execute("SELECT value FROM metadata WHERE key='correction_schema_v1'").fetchone()
    if correction is not None and correction[0] != "1":
        raise ValueError("관리자 정정 이관 기록이 일치하지 않습니다.")
    apply_extension(db, "contact_schema.sql", old is not None,
                    "correction_schema.sql" if correction else None)
    if old is None:
        db.execute("INSERT INTO metadata VALUES ('contact_recipient_v1', ?)", (key_id,))
