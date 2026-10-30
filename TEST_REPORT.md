# V1.4 자체 검수 결과

아래 표는 **Git 도입 전 V1.4 기준 자료**입니다. GitHub 공개 준비 이후의 변경·실행 결과는 마지막 절을 함께 확인하세요.

검수일: 2026-10-30 / 실행 환경: macOS, CPython 3.13.5 새 가상환경, Node.js, portable PowerShell 7.4.6. 실제 카페 Windows 노트북·도메인·Cloudflare 계정·iPhone/Galaxy에는 설치/접속하지 않았습니다.

## 실행 결과

| 검사 | 결과 | 범위/한계 |
|---|---|---|
| Python 전체 | **70 passed** | 기존 44개 유지 + 운영 추가 26개. 모두 임시 DB/복제 소스 사용 |
| JavaScript 기존 | **41 passed** | 중앙 QR 판독·재방문·수집/보상 UI 회귀. 휴대폰 카메라 실측 아님 |
| PowerShell 구문 | **13개 모두 통과** | macOS PowerShell 7.4.6 파서. Windows PowerShell 5.1 실행 결과 아님 |
| PowerShell 순수 보조 함수 | **3개 통과** | 공백 인자 인용·잘못된 인자 거부·경로 대소문자별 작업 ID 일관성 |
| Windows 배포 의존성 | **CPython 3.13 / 3.12 x64용 각각 10개 wheel 확보** | PyPI 패키지 제공/해결 확인. Windows 런타임 실행은 미확인 |
| V1.3 보존 | **앱 40개 파일 원본 ZIP과 동일** | UI/API/DB 스키마/원래 이벤트 예제 바이트 일치 |
| 기존 작업 데이터 | **5개 보존 대상 해시 일치** | 기존 DB 2개·백업 1개·event.json·원본 V1.3 ZIP. 실제 고객 기록을 변경하는 테스트 없음 |

근거 파일: [Python 결과](docs/evidence/python-tests.txt), [JavaScript 결과](docs/evidence/javascript-tests.tap), [PowerShell 결과](docs/evidence/powershell-checks.txt), [원본/의존성 보존 확인](docs/evidence/package-preservation.json).

## 운영 추가 테스트의 실제 범위

- 공백/한글 파일 경로, 기존 DB 읽기 전용 검사, 반복 초기화 요청 시 기존 기록 불변, 다른 이벤트 DB 거부.
- draft/잘못된 HTTPS 주소의 초기화 거부. 카페 설정의 미확정 값으로 서버 공개를 강행하지 않음.
- SQLite API 백업: 쓰기 트랜잭션이 있는 DB의 스냅샷·무결성, 기존 목적지 거부, 임시 파일 정리.
- 일일 14일/ISO 연도 경계의 주간 4주 정리, 외부 사본, 같은 주 1개 보존, 외부 실패 시 로컬 보존/정리 중지.
- 공간 부족 시 기존 백업 불변, 파일 잠금으로 겹친 백업/서버 차단.
- 별도 후보 복구 중 운영 DB 불변, 손상/수정된 후보 거부, 명시적 확인·정지 조건.
- 실제 활성화 시 원본 보존, 파일 교체 실패 시 원본 경로 롤백, 복원 후 복구 토큰·지급 1회 상태 유지.
- 기존 쿠키 재인식, 상품 중복 적립 거부, QR 키/복구 링크/보상 기록 유지.
- 실제 loopback Waitress 검사: 복제 DB만 사용, 정상 종료, 다른 작업 디렉터리에서 실행, 중복 프로세스 거부, 중지·재시작 후 쿠키 기록 유지.
- 업데이트의 후보 앱 오류가 실제 DB에 영향을 주지 않는지 Python 경로 검사. **PowerShell 업데이트 전체 파일 교체/예약 작업 통합은 Windows 미검증**.
- 예약 작업·token-file 사용 등 스크립트의 필수 설정을 정적 확인. 이 검사는 실제 Windows API 실행을 대체하지 않음.

최종 통합 검사에서 다른 스레드가 소켓을 닫을 때 정상 종료가 드물게 오류를 반환하는 문제를 발견했습니다. 운영 래퍼의 Waitress 이벤트 루프와 종료를 같은 스레드에서 처리하도록 수정했고, 전체 테스트를 다시 통과했습니다. 고객용 앱 코드는 변경하지 않았습니다. 종료/재시작 검사 2개를 5회 반복해 모두 통과했습니다. [반복 결과](docs/evidence/http-repeat.txt)를 보관합니다.

## Windows/현장 미검증 항목

다음은 **미검증**입니다: Windows PowerShell 5.1의 전체 설치 실행, UAC·NTFS ACL·SYSTEM 실행, Windows 재부팅 후 로그인 전 시작, 작업 스케줄러 장애 복구, cloudflared native SCM 서비스 등록/재연결, 실제 HTTPS/DNS/Host 전달, Windows 파일 잠금·UNC 공유, 실제 USB 백업/용량 부족, PowerShell 업데이트 전체 성공·롤백, iPhone/Galaxy 카메라·쿠키·인앱 브라우저.

이 항목은 [Windows 인수 검증표](docs/WINDOWS_ACCEPTANCE.md)에 절차/통과 조건을 정리했습니다. 30명/400명 관련 기존 앱 테스트는 유지했지만 실제 Windows 장비/인터넷의 용량·지연·한 달 무중단을 보증하는 부하 시험은 하지 않았습니다.

## 재현 명령 (개발용 별도 폴더)

운영 가상환경에 테스트 의존성을 추가하지 말고 복제한 패키지의 별도 검증 환경에서 실행합니다.

```powershell
python -m venv .test-venv
& ".\.test-venv\Scripts\python.exe" -m pip install -r requirements-test.lock
$env:MAISON_HTTP_TESTS='1'
& ".\.test-venv\Scripts\python.exe" -m pytest -q app/tests tests
node --test app/tests/scanner.test.cjs app/tests/app-flow.test.cjs
& .\tests\Check-PowerShell.ps1
```

`MAISON_HTTP_TESTS`가 없으면 네트워크 검사 2개는 명시적으로 skip됩니다. Node는 JavaScript 검증에만 필요하며 카페 운영 설치의 필수 요소가 아닙니다. PowerShell 검사 파일 자체도 Windows 기능 호출 없이 파싱/순수 함수만 확인합니다. Windows 인수 표의 실동작 검사를 별도로 해야 합니다.

## GitHub 공개 준비 검사 (기존 V1.4 이후)

공개용 저장소는 기존 운영 스크립트를 유지하고 Git/CI/문서/릴리스 빌더를 추가합니다. 첫 Windows CI에서 초기화 DB 연결 잔류와 테스트의 줄바꿈 가정을 발견해 앱 `init_db`의 명시적 연결 종료와 테스트 fixture 정리를 보완했습니다. 단일 프로세스의 동시 쓰기 요청 경합도 조정했습니다. API·스키마·적립·복구·보상 정책은 그대로입니다. 별도 `tests/test_repository.py`에서 공개 파일 차단, 삭제 후에도 남은 Git 이력의 합성 토큰 검출, 커밋된 트리만으로 ZIP 생성, BOM/CRLF 보존, 실제 파일 해시/manifest, 기존 릴리스 덮어쓰기 거부를 검사합니다.

CI 정의는 Ubuntu/Windows hosted runner의 Python 3.12·3.13, 프런트 구문·41개 흐름 검사, Windows PowerShell 파서/보조 함수, 공개 이력 검수입니다. GitHub에서 실제 실행된 결과는 Actions 링크와 별도 전달 기록으로 확인합니다. 이 문서의 기존 70개 통과는 V1.4 로컬 기준이며, 새 공개용 소스의 macOS / CPython 3.13.5 로컬 검사는 **84 passed**(기존 70 + 추가 14)였습니다. JavaScript 41개 및 PowerShell 파서 13개/보조 함수 3개 검사도 통과했습니다. 실제 Windows 10 설치/운영 검증으로 간주하지 않습니다.
