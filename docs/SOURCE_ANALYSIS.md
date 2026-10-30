# V1.3 실제 코드 확인 결과

이 문서는 초기 V1.4 분석 자료입니다. 공개 Git 버전에서는 Windows CI 결과에 따라 `app/app.py`의 `init_db`에 명시적 연결 종료를 추가했습니다. 나머지 V1.3 앱 파일과 운영 스크립트는 유지하며 API/스키마/업무 정책은 바꾸지 않았습니다.

## 기준 소스와 보존 범위

기존 `cafe-collection-mvp-v1.3.zip` 안의 `cafe-collection/` 40개 파일을 `app/`으로 그대로 복사했습니다. 현재 작업 폴더의 실제 SQLite·복구용 백업·모바일 인증서는 패키지에서 제외했습니다. 실제 기존 event.json과 DB를 수정하지 않습니다. 별도 Git 저장소는 이 작업 폴더에서 확인되지 않았습니다.

## manage.py 실제 옵션

```powershell
python manage.py --help
python manage.py --config "C:\MaisonCafe\config\event.json" --database "C:\MaisonCafe\data\collection.sqlite" serve --public-url "https://확정도메인" --port 8765
python manage.py --config "C:\MaisonCafe\config\event.json" --database "C:\MaisonCafe\data\collection.sqlite" backup "C:\백업폴더\새파일.sqlite"
python manage.py --config "C:\MaisonCafe\config\event.json" --database "C:\MaisonCafe\data\collection.sqlite" qr-export --public-url "https://확정도메인" --out "C:\MaisonCafe\qr-export"
```

`--config`, `--database`는 하위 명령 앞에 둡니다. `serve`에는 `--public-url`, `--port`, `--dev`가 있습니다. `--host` 옵션은 없으며 코드에서 `127.0.0.1`로 고정합니다. 이번 운영 스크립트는 `--dev`를 사용하지 않습니다. 날짜·상품·draft 유효성은 앱의 기존 `load_event`를 재사용합니다. 실제 QR 키는 DB에 저장되어 있으므로 QR 내보내기는 반드시 기존 DB 경로를 명시합니다.

기존 `backup`도 SQLite `Connection.backup()`을 사용하고 목적지 기존 파일을 거부합니다. 이번 `ops.py`는 같은 SQLite API에 회차별 무결성·외래키 검사, 보존 정책, 외부 경로, 실패 기록을 더합니다. 운영 DB를 파일 복사하지 않습니다.

## 경로·서버

기존 기본값은 `app/event.json`, `app/data/collection.sqlite`이며 운영 기본값으로 사용하면 앱 교체 시 데이터 혼합 위험이 있습니다. V1.4는 외부 경로를 명시해 기존 `create_app(event_path, database, public_url)` 팩토리를 호출합니다. Waitress `create_server`로 종료 처리를 제공하며 Flask 디버그 서버를 호출하지 않습니다.

Waitress 32 스레드, loopback 신뢰 프록시 1단, `X-Forwarded-For`/`X-Forwarded-Proto`, 본문 제한 4096, 연결 제한 256, 채널 타임아웃 30초와 예외 상세 비노출을 유지합니다. 새 전역 비밀키를 만들거나 API 인증 방식을 변경하지 않습니다. 앱 schema.sql·고객/세션/스탬프/복구/보상 처리 코드는 그대로입니다.

DB의 metadata 이벤트 서명은 `[이벤트 ID, 정렬된 상품 ID 목록]`입니다. 기존 DB를 검사할 때 `init_db`를 호출하지 않고 읽기 전용 연결로 이 서명과 무결성을 확인합니다. 새 DB에 한해서 기존 `init_db`를 호출합니다. **새 DB 초기화 시 앱이 만드는 10개 QR 키는 기존 앱의 필수 동작**이며 운영 인증정보/터널 토큰을 임의 생성하는 것과 구별됩니다.

기존 보안 쿠키는 HTTPS 호스트 기준 `__Host-` 쿠키입니다. Cloudflare 원본은 HTTP loopback이어도 고객 연결은 HTTPS여야 합니다. 원래 앱의 Host/Origin/CSRF/GET 읽기 전용 정책은 변경하지 않습니다.

## Python·Windows

기존 잠금 파일에는 테스트 도구와 Python 3.9 호환용 보조 의존성도 포함되어 있습니다. 운영에는 런타임 10개 패키지만 정확한 버전으로 고정했습니다. Windows의 Click 의존성인 colorama 0.4.6을 추가했습니다. Flask 3.1.2, Waitress 3.0.2, qrcode 8.2 및 기존 전이 의존성 버전은 동일합니다.

Python 3.13.5 macOS의 새 가상환경에서 기존·추가 테스트를 실행했고, CPython 3.13/3.12 Windows x64의 모든 런타임 wheel을 다운로드해 배포 파일 제공을 확인했습니다. 이것은 Windows 실행 검증이 아닙니다. Waitress 3.0.2의 Python 3.13 지원 표기는 [배포 메타데이터](https://pypi.org/project/waitress/3.0.2/)와 함께 확인했습니다.

Windows는 POSIX chmod만으로 고객 DB·토큰을 보호할 수 없어 별도 NTFS ACL을 적용합니다. SYSTEM 자동 실행을 위해 사용자 프로필·Store·Conda Python을 거부하고 모든 사용자용 CPython을 요구합니다. PowerShell은 5.1 문법/기본 Windows cmdlet을 사용하며 별도 PowerShell 7 설치가 필요하지 않습니다. Windows 전용 cmdlet의 실제 동작은 실기기 검증 대상입니다.

공백/한글 경로는 Path·LiteralPath·명시적 인자 인용을 사용합니다. 로컬 NTFS 설치만 허용하고 정션·심볼릭 링크는 거부합니다. 실제 Windows의 공백 경로·권한·UNC 공유 동작은 검증표에 따릅니다. 기본 짧은 경로를 권장하며 긴 경로(Windows 260자 제한 관련)는 보장하지 않습니다.
