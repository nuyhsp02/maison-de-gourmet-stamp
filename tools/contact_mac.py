"""Separate DEMO and PROD recipients and interactive single-card viewing on the designated Mac."""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import warnings

from nacl import pwhash, secret, utils
from nacl.public import PrivateKey, SealedBox

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import contact_privacy as contact


def write_new(path, data):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def custody_output(path):
    """Private key copies and PROD provisioning descriptors stay in a separate owner-only directory."""
    path = Path(path)
    parent = path.parent
    if (not path.is_absolute() or parent.resolve(strict=True) != parent or path.exists() or path.is_symlink()
            or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077
            or path.is_relative_to(Path(__file__).resolve().parents[1])
            or any((folder / ".git").exists() for folder in path.parents)):
        raise ValueError("저장소 밖의 전용 비공개 디렉터리에 새 파일을 지정하세요.")


def lock_key(private, password):
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    derived = derive(password, salt)
    return b"MDC1" + salt + secret.SecretBox(derived).encrypt(bytes(private))


def derive(password, salt):
    return pwhash.argon2id.kdf(secret.SecretBox.KEY_SIZE, password, salt,
                               opslimit=pwhash.argon2id.OPSLIMIT_MODERATE,
                               memlimit=pwhash.argon2id.MEMLIMIT_MODERATE)


def unlock_key(blob, password):
    size = pwhash.argon2id.SALTBYTES
    if len(blob) != 4 + size + secret.SecretBox.NONCE_SIZE + secret.SecretBox.MACBYTES + PrivateKey.SIZE or blob[:4] != b"MDC1":
        raise ValueError("잘못된 키 파일")
    return PrivateKey(secret.SecretBox(derive(password, blob[4:4+size])).decrypt(blob[4+size:]))


def open_phone(private, reply, event_id, cid):
    kid = hashlib.sha256(bytes(private.public_key)).hexdigest()
    if reply["key_id"] != kid or reply["event_id"] != event_id or reply["customer_id"] != cid:
        raise ValueError("수신 키 또는 카드 불일치")
    message = SealedBox(private).decrypt(base64.b64decode(reply["sealed"], validate=True))
    version, event, owner, digits = json.loads(message)
    expected = contact.PROD_CONTACT if event_id.startswith("prod-") else contact.NOTICE
    if (version, event, owner) != (expected, event_id, cid) or contact.phone_digits(digits) != digits or not digits:
        raise ValueError("연락처 확인 실패")
    return digits[:3] + "-" + digits[3:-4] + "-" + digits[-4:]


def prompt_password(label, confirm=False):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(label)
        if confirm and (len(value) < 15 or value != getpass.getpass("다시 입력: ")):
            raise ValueError("암호 길이 또는 확인 오류")
    return value.encode()


def read_private(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("키 파일 권한 오류")
    return unlock_key(path.read_bytes(), prompt_password("키 보관 암호: "))


def view(args, private):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", args.host):
        raise ValueError("SSH 호스트 별칭 오류")
    cid = input("카드 ID: ").strip()
    purpose = input("용도 (pickup/reward/inquiry): ").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", cid) or purpose not in ("pickup", "reward", "inquiry"):
        raise ValueError("조회 대상 또는 용도 오류")
    command = ["ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oClearAllForwardings=yes",
               "-oForwardAgent=no", "-oIdentitiesOnly=yes", "-oControlMaster=no", "-oControlPath=none",
               "-oConnectTimeout=10"]
    if args.ssh_config:
        command.extend(["-F", str(args.ssh_config)])
    command.append(args.host)
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True) as channel:
        try:
            channel.stdin.write(json.dumps({"customer_id": cid, "purpose": purpose}) + "\n")
            channel.stdin.flush()
            reply = json.loads(channel.stdout.readline(4096))
            if reply["status"] == "unavailable":
                print("연락처 미보관")
                return
            if reply["status"] != "released":
                raise ValueError("조회 실패")
            try:
                phone = open_phone(private, reply, args.event, cid)
            except Exception:
                channel.stdin.write('{"result":"decrypt_failed"}\n')
                channel.stdin.flush()
                raise
            # Alternate screen avoids adding the number to normal terminal scrollback.
            with open("/dev/tty", "w", encoding="utf-8") as tty:
                try:
                    tty.write("\033[?1049h\033[2J\033[H" + phone + "\n")
                    tty.flush()
                    channel.stdin.write('{"result":"displayed"}\n')
                    channel.stdin.flush()
                    input("확인 후 Enter: ")
                finally:
                    tty.write("\033[2J\033[?1049l")
                    tty.flush()
        finally:
            channel.stdin.close()
        if channel.wait(timeout=10) != 0:
            raise ValueError("운영 조회 결과 확인 실패")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    create = subs.add_parser("keygen", help="DEMO synthetic recipient")
    prod = subs.add_parser("keygen-prod", help="New PROD contact recipient on the designated Mac")
    for keygen in (create, prod):
        keygen.add_argument("--public-config", type=Path, required=True)
        keygen.add_argument("--event", required=True)
    show = subs.add_parser("show")
    show.add_argument("--host", required=True, help="Pinned operator SSH alias")
    show.add_argument("--event", required=True)
    show.add_argument("--ssh-config", type=Path, help="Operator-managed SSH configuration with a pinned host key")
    copy = subs.add_parser("rewrap", help="Create an independently encrypted backup or restored private key")
    copy.add_argument("--output", type=Path, required=True)
    for cmd in (create, prod, show, copy):
        cmd.add_argument("--key", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "darwin" or not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        parser.exit(1, "지정 Mac의 대화형 터미널에서 실행하세요.\n")
    try:
        if args.action in ("keygen", "keygen-prod"):
            production = args.action == "keygen-prod"
            prefix = "prod-" if production else "demo-"
            if not re.fullmatch(prefix + r"[a-z0-9][a-z0-9-]{0,58}", args.event):
                raise ValueError("행사 식별 정보 불일치")
            if production:
                custody_output(args.key)
                custody_output(args.public_config)
            private = PrivateKey.generate()
            encrypted = lock_key(private, prompt_password("새 키 보관 암호 (15자 이상): ", True))
            policy = {"format": "prod-contact-recipient-v1" if production else contact.NOTICE,
                      "event_id": args.event, "public_key": bytes(private.public_key).hex()}
            if not production:
                policy["allowed_profiles"] = [
                    contact.profile_tag("합성 검수 " + str(i), head + str(i).zfill(size))
                    for i, (head, size) in enumerate([("010", 8), ("011", 7), ("019", 8)], 1)]
            write_new(args.key, encrypted)
            try:
                write_new(args.public_config, json.dumps(policy).encode())
            except BaseException:
                args.key.unlink()  # This invocation created the file exclusively.
                raise
        elif args.action == "rewrap":
            custody_output(args.output)
            private = read_private(args.key)
            password = prompt_password("새 보관 사본 암호 (15자 이상): ", True)
            blob = lock_key(private, password)
            if bytes(unlock_key(blob, password)) != bytes(private):
                raise ValueError("키 검증 실패")
            write_new(args.output, blob)
        else:
            view(args, read_private(args.key))
    except (Exception, KeyboardInterrupt):
        parser.exit(1, "처리 중단. 권한·키·대상·감사 기록을 확인하세요.\n")


if __name__ == "__main__":
    main()
