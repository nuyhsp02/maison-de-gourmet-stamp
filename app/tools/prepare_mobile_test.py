#!/usr/bin/env python3
"""Prepare isolated mobile-test files. Does not start servers, touch a DB or trust a CA."""
import argparse
import copy
import ipaddress
import json
import os
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def prepare(source, destination, origin, port=8875):
    if any(ord(c) <= 32 for c in origin):
        raise ValueError("테스트 URL에 공백이나 줄바꿈을 넣을 수 없습니다.")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("예: https://192.168.0.20:8443 형식의 HTTPS 원본 주소가 필요합니다.")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("휴대폰의 localhost는 노트북이 아닙니다.")
        labels = host.split(".")
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                      for label in labels):
            raise ValueError("올바른 노트북 IP 또는 호스트명을 지정하세요.")
    else:
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("휴대폰에서 노트북에 도달할 수 있는 주소를 사용하세요.")
    tls_port = parsed.port
    if tls_port is None or not 1024 <= tls_port <= 65535:
        raise ValueError("실기기 테스트는 :8443 등 1024 이상 전용 HTTPS 포트를 명시하세요.")
    if not 1024 <= port <= 65535 or port == tls_port:
        raise ValueError("앱 포트는 1024 이상이며 HTTPS 포트와 달라야 합니다.")
    origin = f"https://{parsed.netloc.lower()}"
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if any(c in str(destination) for c in ('\n', '\r')):
        raise ValueError("출력 경로에 줄바꿈을 넣을 수 없습니다.")
    original = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(original, dict) or len(original.get("products", [])) != 10:
        raise ValueError("상품 10개를 포함한 기존 event.json이 필요합니다.")
    event = copy.deepcopy(original)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # Test window only. Original event.json and all IDs/product definitions remain intact.
    event.update(draft=False, starts_at=(now - timedelta(minutes=5)).isoformat(),
                 ends_at=(now + timedelta(days=30)).isoformat(),
                 reward_until=(now + timedelta(days=31)).isoformat())
    os.umask(0o077)
    destination.mkdir(parents=True, exist_ok=False)
    event_path = destination / "event.json"
    database = destination / "collection.sqlite"
    caddy_path = destination / "Caddyfile"
    storage = destination / "caddy-data"
    event_path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    caddy_path.write_text(
        "# Local mobile test only. No public deployment or access log.\n"
        "{\n    admin off\n    persist_config off\n    skip_install_trust\n"
        "    auto_https disable_redirects\n"
        f"    storage file_system {json.dumps(str(storage), ensure_ascii=False)}\n}}\n\n"
        f"{origin} {{\n    tls internal\n    reverse_proxy 127.0.0.1:{port}\n}}\n", encoding="utf-8")
    python = shlex.quote(sys.executable)
    base = f"{python} {shlex.quote(str(ROOT / 'manage.py'))} --config {shlex.quote(str(event_path))} --database {shlex.quote(str(database))}"
    run = (
        "# 휴대폰 테스트 전용 — 운영 설정과 DB를 사용하지 않습니다.\n\n"
        "노트북과 휴대폰을 같은 Wi-Fi에 연결하세요. 포트만 다르게 해도 쿠키는 공유될 수 있으므로 "
        "운영과 다른 호스트 또는 분리한 테스트 브라우저 저장소를 사용하세요.\n\n"
        "각 명령을 별도 터미널에서 실행합니다. 아직 서버는 시작되지 않았습니다.\n\n"
        f"```sh\n{base} serve --public-url {shlex.quote(origin)} --port {port}\n```\n\n"
        f"```sh\ncaddy run --config {shlex.quote(str(caddy_path))} --adapter caddyfile\n```\n\n"
        "Caddy 실행 후 생성되는 아래 공개 루트 인증서만 테스트 기기에 설치·신뢰합니다. "
        "인증서 경고를 무시하고 진행하지 마세요. 시스템 신뢰를 자동 변경하지 않습니다.\n\n"
        f"`{storage / 'pki/authorities/local/root.crt'}`\n\n"
        "root.key 등 개인 키, DB, 복구 링크는 공유하지 마세요. 자세한 기기별 절차는 MOBILE_TEST_GUIDE.md에 있습니다.\n\n"
        f"휴대폰 접속: {origin}\n\n"
        "신뢰와 연결을 확인한 뒤 테스트용 QR을 생성합니다.\n\n"
        f"```sh\n{base} qr-export --public-url {shlex.quote(origin)} --out {shlex.quote(str(destination / 'qr'))}\n```\n\n"
        "테스트 기간은 생성 시점부터 약 30일로 잡은 임시값이며 실제 행사 일정이 아닙니다. "
        "준비 폴더를 다시 덮어쓰지 않습니다. 노트북 IP가 바뀌면 새 폴더에서 준비하고 인증서와 QR을 다시 확인하세요.\n"
    )
    (destination / "RUN.md").write_text(run, encoding="utf-8")
    return destination


def main():
    parser = argparse.ArgumentParser(description="원본을 보존하고 실기기 검증용 설정 사본만 준비합니다.")
    parser.add_argument("--origin", required=True, help="노트북 LAN IP의 HTTPS 주소. 예: https://192.168.0.20:8443")
    parser.add_argument("--out", required=True, help="존재하지 않는 새 테스트 폴더")
    parser.add_argument("--config", default=str(ROOT / "event.json"))
    parser.add_argument("--port", type=int, default=8875)
    args = parser.parse_args()
    try:
        directory = prepare(args.config, args.out, args.origin, args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"테스트 파일 준비 완료: {directory / 'RUN.md'}")
    print("서버 실행·인증서 설치·기존 DB 변경·실제 배포는 수행하지 않았습니다.")


if __name__ == "__main__":
    main()
