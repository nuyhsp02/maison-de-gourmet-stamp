# 현장 검증 도구와 시험 환경 준비

이 문서는 [인수 계획](WINDOWS_ACCEPTANCE.md)을 실행하기 위한 안내입니다. **PR #4 Draft·미병합 유지. 카페 배포/정식 Release/태그 변경 없음.** 도구의 합성 HTTP 검사와 Windows 10·HTTPS·휴대폰 인수 결과를 구분합니다. [결과 양식](evidence/FIELD_ACCEPTANCE_TEMPLATE.md)에 실제로 수행한 것만 적습니다.

## 재사용한 도구와 추가 범위

| 목적 | 도구 / 범위 |
|---|---|
| 고정 소스 후보 ZIP·SHA256·manifest | 기존 `tools/build_release.py`. 커밋된 public tree만 포함, DB/실제 설정 없음. 후보 라벨은 로컬 파일 식별자이며 Git 태그/GitHub Release 생성이 아님 |
| 런타임/Windows 설치/권한/부팅/관리 | 기존 `check_sqlite_runtime.py`, `Install.ps1`, `Register-Autostart.ps1`, `Control-Server.ps1` 재사용. [설치 안내](WINDOWS_INSTALL.md) 적용 |
| 백업/복원/HTTP 서버 | 기존 `scripts/ops.py`의 validate/create_http/run_http/close_http/backup/stage_restore/activate_restore/selftest 재사용. 운영 구현 변경 없음 |
| 부족했던 시험 자동화 | **`tools/field_http.py` 하나**: public 예제로 시험 설정 준비, 새 전용 폴더의 실제 Waitress HTTP 검사. 운영 앱/API/DB 스키마/15초는 그대로 |

`prepare`는 시험 설정만 생성하며 DB·서버·TLS·서비스를 만들지 않습니다. `run`은 **존재하지 않는 새 출력 폴더**에 공개 앱 파일만 복사하고 자체 시험 DB/백업/로그를 만듭니다. 기존 폴더·심볼릭 링크 경로를 거부하며 기존 DB나 원격 서버를 지정하는 옵션은 없습니다. manifest가 있으면 기존 패키지 검사를, 개발 clone이면 Git 관리 public 앱 파일 검사를 사용합니다. 운영 config/data/QR 키는 읽거나 복사하지 않습니다. 같은 도구가 운영 루트에서 실행돼도 원본 DB를 시험 대상으로 삼지 않습니다.

## 1. 받을 파일과 장비 확인 — 사용자 수행

최종 보고에 제공한 **후보 Windows ZIP과 SHA256 파일**을 시험 장비로 복사해 [기존 해시 확인/압축 해제](GITHUB_RELEASES.md#다운로드무결성반영) 절차를 사용합니다. 기존 v1.4.0 다운로드나 GitHub 자동 Source ZIP으로 대체하지 않습니다. manifest의 source_commit과 보고서의 전체 SHA가 일치해야 합니다. 패키지 라벨이 `v1.4.1-field-…`여도 정식 v1.4.1이 발행된 것은 아닙니다.

아래는 **별도 Windows 10 22H2 x64 시험 장비/VM**용 예시입니다. 현재 실제 장비에 설치한 결과가 아닙니다. 업무용 ESU·보안 업데이트·NTFS·전원·SYSTEM 전제는 [인수 계획](WINDOWS_ACCEPTANCE.md#1-격리와-실행-전-준비)을 먼저 확인합니다. 기존 Cloudflared 서비스가 있는 운영 장비에서 서비스를 바꾸거나 삭제하지 않습니다.

## 2. 공식 런타임과 전용 venv

[설치 안내의 공식 NuGet 공급](WINDOWS_INSTALL.md#검증된-전체-런타임을-별도-경로에-준비)에 따라 공식 Python 3.15.2 **전체** 배포를 새 공유 경로에 받습니다. 아래 경로는 압축 해제한 실제 위치로 맞춥니다. 64비트 관리자 PowerShell 5.1에서 한 명령씩 실행하고 오류/비정상 종료 코드가 있으면 중단합니다.

```powershell
$package = 'C:\Maison Field Package\maison-cafe-v1.4'
$basePython = 'C:\MaisonPython3152\python.3.15.2\tools\python.exe'
$trialRoot = 'C:\MaisonCafe-Acceptance'
& $basePython "$package\tools\check_sqlite_runtime.py" --require-fixed
& "$package\scripts\Install.ps1" -Root $trialRoot -PythonExe $basePython -PrepareOnly
$trialPython = "$trialRoot\.venv\Scripts\python.exe"
& $trialPython "$package\tools\check_sqlite_runtime.py" --require-fixed
& $trialPython "$trialRoot\scripts\ops.py" --root $trialRoot check-runtime
```

sys.version=3.15.2 / CPython x64/AMD64/GIL / SQLite=3.53.4 및 [기대 source ID](WINDOWS_ACCEPTANCE.md#실제-런타임과-권한)를 각각 확인합니다. Python 버전만으로 판정하지 않습니다. 설치된 Python/기존 venv를 자동 교체하지 않습니다. 준비 설치에는 서버/DB/예약 작업 생성이 없습니다. 운영 lock의 wheel-only 설치 정책은 기존 설치기가 유지합니다.

## 3. 지금 실행 가능한 격리 HTTP 도구

시험 venv로 **새 경로**를 지정합니다. 기존 고객/기존 시험 결과를 초기화하지 않으므로 재실행할 때도 새 폴더명을 사용합니다. 도구 때문에 반복 횟수를 늘리지 말고 한 회차의 결과를 먼저 검토합니다.

```powershell
& $trialPython "$package\tools\field_http.py" run --out 'C:\MaisonFieldRuns\field-01'
```

대략 수 분이 걸립니다. 5·10·30 고객 단계마다 각 5회 burst, 회차 완료 뒤 10초 휴식입니다. 로컬 Waitress를 임시 포트/127.0.0.1/32 threads/TESTING=false로 시작합니다. 실물 브라우저가 아닌 독립 HTTP 세션을 사용합니다. 클라이언트의 새 20초 소켓 제한은 서버의 기존 write_lock/SQLite 15초나 Waitress 설정을 바꾸지 않고 15초 응답을 관찰하기 위한 것입니다.

| 검사 | 확인하는 내용 / 제외 범위 |
|---|---|
| 정상·중복·미완성 지급 | 실제 QR GET의 무변경, 새 카드·10종·중복, 미완성 지급 거부. 실제 QR 카메라/UI 검사는 별도 |
| 쿠키·복구·재발급 | HTTP 쿠키 재사용, 다른 세션의 복구, 회전된 옛 링크 거부. 실제 브라우저 저장/종료·인앱 차이는 별도 |
| 동시 HTTP | 5/10/30 독립 세션별 5종 일치, 상태/지연/오류별 집계. 30실물 단말 검증이나 원본 5200 POST 부하의 대체가 아님 |
| 지급 응답 유실 | 테스트 전용 WSGI 중계가 실제 앱의 200 완료 뒤 응답을 보류. DB 지급 기록 확인 후 고객 소켓을 끊음. 재조회/redeem 재요청에도 지급 감사 1. 실제 실물 지급/화면 검사는 별도 |
| admission 503 | 같은 시험 앱의 native Lock을 제어 스레드가 최대 20초 보유. 원래 15초 경로의 503/미적립 확인 후 해제·GET·수동 1회 동작을 실행. **의도적 오류이며 과거 간헐 원인 재현 아님** |
| 재시작·백업/복원 | 실제 Waitress 정상 중지/재시작, SQLite API 백업, 별도 복구 후보·현재 DB 불변, 시험 원본 보존 후 T0 활성화·HTTP 재조회. Windows 감독/서비스/전원 차단은 별도 |

성공 종료 코드 0과 `field-tool-report.json`의 `tool_checks_passed=true`를 함께 확인합니다. 완료 시 **합성 고객 47 / 스탬프 237 / 지급 감사 1**, 무결성/FK 정상·중복/지급 불일치 0이어야 합니다. T0 이후 시험 스탬프 1개는 복원 후 없어지는 예상 차이입니다. 중간 실패의 부분 카운트를 완주로 표시하지 않습니다. 실패는 종료 코드 1이며 해당 폴더를 지우거나 실패 로그를 성공 결과로 덮지 않습니다.

보고서는 실제 런타임/소스, 단계별 요청 수·경과, HTTP 경로 **종류**별 지연/오류, 완료 사례와 실패 분류만 기록합니다. 토큰/QR/쿠키/CSRF/본문/고객 ID는 넣지 않습니다. DB/백업은 합성이라도 public 저장소에 올리지 않습니다. 지연 통계는 측정값이며 미승인 인수 기준을 자동 승인하지 않습니다. `field_acceptance=not_performed`는 성공한 Windows 도구 실행에서도 유지됩니다.

> 로컬 HTTP 검사는 예약된 시험 Host와 X-Forwarded-Proto 헤더를 **loopback 안에서만** 사용하고 HTTP 세션이 Secure 쿠키를 직접 운반합니다. 실제 TLS·브라우저 Secure 쿠키·카메라 권한 성공을 뜻하지 않습니다. localhost 수치를 LTE 성능으로 쓰지 않습니다.

macOS 개발자는 `run --out 새경로 --tooling-smoke`로 도구만 확인할 수 있습니다. 실제 엔진이 목표와 다르면 보고서에 남으며 운영 런타임/Windows 승인이 아닙니다. Windows에서 이 옵션으로 미검증 런타임 제한을 우회할 수 없습니다.

## 4. 모바일용 시험 설정·DB·QR 준비

운영과 다른 시험 hostname을 먼저 확정합니다. 아래 URL은 예시이고 실제 접속 주소가 아닙니다. 공개/연결 작업은 담당자가 별도로 수행합니다. `.invalid` 로컬 도구 origin은 외부 휴대폰 주소가 아닙니다.

```powershell
$testOrigin = 'https://stamps-test.example.com' # 소유한 시험 hostname으로 교체
$settings = 'C:\MaisonFieldSettings\field-01'
& $trialPython "$package\tools\field_http.py" prepare --out $settings --origin $testOrigin --port 8875 --metrics-port 20242
```

설정 생성 후 외부 연결 전까지는 hostname으로 접속할 수 없습니다. 다음 복사는 **방금 준비 설치한 미설정 시험 Root**에만 합니다. event/DB/설정 백업이 있거나 이미 PublicUrl이 채워져 있으면 멈춥니다. 기존 설정 원본도 따로 보존합니다.

```powershell
if ($trialRoot -ne 'C:\MaisonCafe-Acceptance') { throw '시험 Root 확인 필요' }
if ((Test-Path "$trialRoot\data\collection.sqlite") -or (Test-Path "$trialRoot\config\event.json") -or (Test-Path "$trialRoot\config\operations.pre-field.json")) { throw '기존 시험/운영 상태를 보존합니다. 새 환경을 사용하세요.' }
$old = Get-Content "$trialRoot\config\operations.json" -Raw -Encoding UTF8 | ConvertFrom-Json
if ($old.PublicUrl) { throw '이미 설정된 환경은 변경하지 않습니다.' }
Copy-Item "$trialRoot\config\operations.json" "$trialRoot\config\operations.pre-field.json"
Copy-Item "$settings\config\event.json" "$trialRoot\config\event.json"
Copy-Item "$settings\config\operations.json" "$trialRoot\config\operations.json"
& "$package\scripts\Install.ps1" -Root $trialRoot -PythonExe $basePython
```

처음 기능 점검에서는 자동 실행 등록 질문에 N으로 답하고, [기존 설치 안내](WINDOWS_INSTALL.md#5-설치-마무리-및-자동-실행)에 따라 별도로 등록/시작할 수 있습니다. 시험 이벤트/상품명에는 TEST 표시가 붙고 운영과 다른 DB에서 새 QR 키가 생성됩니다. 기존 이벤트/키를 가져오지 않습니다.

```powershell
& "$trialRoot\scripts\Register-Autostart.ps1" -Root $trialRoot -StartNow
& "$trialRoot\scripts\Control-Server.ps1" -Root $trialRoot -Action Status
& $trialPython "$trialRoot\app\manage.py" --config "$trialRoot\config\event.json" --database "$trialRoot\data\collection.sqlite" qr-export --public-url $testOrigin --out "$trialRoot\qr-exports"
```

## 5. HTTPS·Windows·휴대폰 — 실제 수행 필요

1. 전용 시험 장비/VM에서 기존 [Tunnel 설정](WINDOWS_INSTALL.md)의 절차를 사용합니다. 시험 hostname → `http://127.0.0.1:8875`, metrics `127.0.0.1:20242`, 새 시험 토큰만 사용합니다. **전역 Cloudflared 서비스가 있으면 설치기가 거절**하므로 기존 서비스를 지우거나 다른 Root로 덮어쓰지 않습니다. 이번 작업에서 DNS/서비스/터널은 만들지 않았습니다.
2. `Control-Server -Action Status`의 local_healthy/tunnel_ready, 인증서 경고 없는 시험 HTTPS, 휴대폰 LTE/다른 Wi-Fi를 확인합니다. 신뢰되지 않는 인증서를 무시하지 않습니다. LAN 자체 인증서 경로는 기존 [모바일 준비 도구/안내](../app/MOBILE_TEST_GUIDE.md)를 선택적으로 재사용하되 현재 Windows 운영 스크립트 방식과 섞지 않습니다.
3. iPhone Safari → Android Chrome 순으로 기본 카메라 첫 QR/새 카드, 재방문, 중복, 복구, 연속 스캔·중앙 판독/복수 QR/권한 거부 대안, 10종/시험 지급, 네트워크 복귀를 [기존 인수표](WINDOWS_ACCEPTANCE.md)에 기록합니다. 인앱은 별도 프로필로 구분합니다.
4. 별도 Windows 10에서 SYSTEM 실제 런타임/NTFS/로그인 전 부팅/전원·절전/터널 재연결을 수행합니다. CI Windows Server 결과를 Windows 10 결과로 표시하지 않습니다.
5. 시험 카드/복구 링크/QR 상태를 기록한 뒤 기존 명령으로 재시작·백업·별도 복원 검증을 진행합니다. Restore -Activate는 [원본 보존·RESTORE 확인·점검 모드 절차](BACKUP_RECOVERY.md)를 **시험 DB에서만** 따릅니다.

```powershell
& "$trialRoot\scripts\Control-Server.ps1" -Root $trialRoot -Action Restart
& "$trialRoot\scripts\Backup.ps1" -Root $trialRoot
& "$trialRoot\scripts\Restore.ps1" -Root $trialRoot -BackupFile '실제로 생성된 시험 백업 파일의 전체 경로'
```

운영자에게 남은 결정은 시험 도메인·장비/보안 상태·승인할 지연/오류 기준·복구 시간/기록 차이·외부 백업 매체·장애 담당자입니다. 간헐 admission 원인 미확정과 직원 인증 없는 지급 위험도 계속 리뷰해야 합니다. 이번 도구 실행이 PR 병합·릴리스·운영 승인은 아닙니다.

## 개발 중 실제 확인과 한계

[로컬 도구 검증 기록](evidence/field-tool-local.json)은 macOS CPython 3.13.5/SQLite 3.45.3의 **도구 smoke**이며 목표 Windows 런타임 결과가 아닙니다. 첫 실행은 재시작 뒤 도구가 이전 서버 객체를 참조해 ConnectionRefusedError로 끝났고, 같은 Server 객체를 재시작하도록 고친 뒤 새 폴더 실행이 백업/복원까지 통과했습니다. 운영 앱은 고치지 않았습니다. 기존 전체 104개 회귀와 JS 41개, 추가 안전 사례 포함 도구 단위 9개를 확인했습니다. 최종 코드는 기존 CI 전체 검사와 별도로, Windows 3.15.2 push의 `[field-tool]` 명시 실행에서 **한 번** 실제 HTTP 도구를 확인하고 비식별 JSON만 보존하도록 준비했습니다. 결과는 해당 실제 CI 링크/최종 보고를 확인하며 미실행 결과를 이 문서에 미리 채우지 않습니다. Windows Server CI 성공도 Windows 10·SYSTEM·HTTPS·실물 카메라 인수는 아닙니다.
