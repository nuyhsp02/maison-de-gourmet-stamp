#!/usr/bin/env python3
"""Laptop operations. Run `python manage.py --help`."""
import argparse
import html
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from app import ROOT, connect_db, create_app, init_db, load_event


def main():
    # Protect database, WAL, backups and operator-only QR exports on this laptop.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="카페 컬렉션 노트북 서버")
    parser.add_argument("--config", default=str(ROOT / "event.json"))
    parser.add_argument("--database", default=str(ROOT / "data" / "collection.sqlite"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="서버 시작")
    serve.add_argument("--dev", action="store_true", help="노트북 localhost HTTP 미리보기 전용")
    serve.add_argument("--public-url", help="고객이 접속할 HTTPS 원본 주소")
    serve.add_argument("--port", type=int, default=8765)
    qr = commands.add_parser("qr-export", help="운영자용 상품별 고정 QR 파일 생성")
    qr.add_argument("--public-url", required=True)
    qr.add_argument("--out", default=str(ROOT / "qr-export"))
    backup = commands.add_parser("backup", help="실행 중에도 안전한 SQLite 백업")
    backup.add_argument("destination")
    args = parser.parse_args()
    if args.command == "serve":
        from waitress import serve as run_server
        url = args.public_url or (f"http://127.0.0.1:{args.port}" if args.dev else None)
        if not url:
            parser.error("실운영은 --public-url https://도메인이 필요합니다.")
        app = create_app(args.config, args.database, url, dev=args.dev)
        print(f"카페 컬렉션: {url}", flush=True)
        print(f"이 노트북의 127.0.0.1:{args.port}에서 실행합니다. 종료: Ctrl+C", flush=True)
        # Caddy must be on the same laptop. Trust only its scheme and IP headers;
        # keep Host unchanged, strip any other forwarded headers.
        proxy_options = {} if args.dev else {
            "trusted_proxy": "127.0.0.1", "trusted_proxy_count": 1,
            "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"}}
        run_server(app, host="127.0.0.1", port=args.port, threads=32,
                   **proxy_options,
                   clear_untrusted_proxy_headers=True,
                   max_request_body_size=4096, channel_timeout=30,
                   connection_limit=256, expose_tracebacks=False)
    elif args.command == "qr-export":
        from qrcode import QRCode
        from qrcode.constants import ERROR_CORRECT_M
        from qrcode.image.svg import SvgPathImage
        url = args.public_url.rstrip("/")
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.path or parsed.query or parsed.fragment or parsed.username):
            parser.error("도메인과 포트만 포함한 URL을 지정하세요.")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"):
            parser.error("localhost 외 주소는 HTTPS가 필요합니다.")
        event = load_event(args.config)
        init_db(args.database, event)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        with connect_db(args.database) as db:
            keys = {p["id"]: p["qr_key"] for p in db.execute("SELECT * FROM products")}
        manifest, cards = [], []
        for product in event["products"]:
            target = url + "/q/" + keys[product["id"]]
            code = QRCode(error_correction=ERROR_CORRECT_M, border=4)
            code.add_data(target)
            code.make(fit=True)
            code.make_image(image_factory=SvgPathImage).save(str(out / (product["id"] + ".svg")))
            manifest.append(dict(id=product["id"], name=product["name"], url=target))
            cards.append(f'<article><h2>{html.escape(product["name"])}</h2>'
                         f'<img src="{product["id"]}.svg" alt="상품 QR"><p>'
                         f'<a href="{html.escape(target, quote=True)}">로컬 동작 확인</a></p></article>')
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "index.html").write_text('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>운영자용 QR 확인</title>'
            '<style>body{font:16px sans-serif;background:#f4f1e8;padding:24px;color:#234a3c}'
            'main{display:flex;flex-wrap:wrap;gap:20px}article{background:white;padding:20px}'
            'img{width:200px;height:200px}h2{font-size:16px}</style>'
            '<h1>운영자용 · 상품 QR 확인</h1><p>구매 인증 기능이 없는 고정 QR입니다. 외부에 공유하지 마세요. '
            '이 페이지는 동작 확인용이며, QR 보드의 실제 크기·덮개·배치 시안이 아닙니다.</p><main>'
            + ''.join(cards) + '</main></html>', encoding="utf-8")
        print(f"고정 QR 10개와 확인용 index.html: {out.resolve()}")
        print("DB를 보존하면 같은 URL이 유지됩니다. 도메인 변경 후에는 QR을 다시 출력하세요.")
    elif args.command == "backup":
        source, target = Path(args.database).resolve(), Path(args.destination).resolve()
        if not source.exists() or target.exists() or source == target:
            parser.error("원본 DB가 존재하고 백업 경로는 새 파일이어야 합니다.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with connect_db(source) as src, connect_db(target) as dst:
            src.backup(dst)
        target.chmod(0o600)
        print(f"백업 완료: {target}")


if __name__ == "__main__":
    main()
