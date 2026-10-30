# DEV-007 · 트랜잭션 중심 쓰기 잠금 구현 후보

상태: main cceed35 기준 별도 브랜치 `fix/transaction-write-scope`. 운영 채택 보류 / [Draft PR #4](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/4). 릴리스·현장 배포 없음.

## 변경과 검토 근거

[DEV-006](DEV-006-windows-concurrency-investigation.md)의 결과를 재사용합니다. 새로 원인 조사를 구축하지 않습니다.

`app/app.py`의 `write_transaction()`은 연결 생성/PRAGMA 뒤 기존 15초로 잠금을 얻고 BEGIN IMMEDIATE → 변경 조건 재검증 → 쓰기 → COMMIT까지 소유합니다. 예외는 BaseException까지 받아 ROLLBACK 후 해제합니다. rollback 실패 시 연결을 잠금 안에서 닫고, 이것도 실패하면 프로세스의 후속 쓰기를 차단합니다. 새 프레임워크·DB 스키마·API 필드·보상 정책은 추가하지 않았습니다.

- session: 신규 카드 기간 재검증, 만료 세션 삭제/고객/세션 생성 한 트랜잭션.
- collect: 세션·CSRF·기간을 소유 후 다시 검증하고 지급 상태 검사/중복 방지 INSERT를 수행.
- recovery-link: 자동 커밋 쓰기를 명시적 트랜잭션으로 변경하고 세션 재검증.
- recover: 복구 토큰 유효성/현재 세션 검사/삭제/새 세션 생성을 같은 트랜잭션에서 처리.
- redeem: 세션 재검증과 완료/기간/지급 감사 기록을 한 트랜잭션으로 처리. 이미 지급된 응답도 commit/해제 후 생성.

snapshot과 정상 close는 잠금 밖입니다. snapshot의 여러 SELECT는 명시적 읽기 트랜잭션으로 일관된 상태를 읽습니다. 결과는 자신의 쓰기 직후 또는 다른 후속 커밋의 상태일 수 있습니다. 응답 added/already_redeemed는 해당 쓰기 결과이고 snapshot은 별도 읽기 시점입니다. 동시 복구가 세션을 폐기하면 이전 화면의 CSRF가 더 이상 유효하지 않을 수 있으며 재조회가 필요합니다. GET은 고객 행을 변경하지 않습니다.

연결의 PRAGMA 설정 실패에도 핸들을 명시적으로 닫습니다. SIGKILL/전원 단절처럼 Python finally가 실행되지 않는 종료를 보장하지 않습니다.

## 런타임: 운영 적용 차단 항목

[SQLite 공식 WAL-reset 자료](https://sqlite.org/wal.html#walreset)의 수정 버전은 3.51.3 이상 또는 공식 백포트 3.50.7/3.44.6 계열입니다. `tools/check_sqlite_runtime.py`는 실제 sqlite_version과 SELECT sqlite_source_id()를 메모리 DB에서 출력합니다. 알려진 수정 버전 범위와 알 수 없는 vendor 백포트를 구분하며 Python 버전만으로 판정하지 않습니다.

```powershell
& 'C:\MaisonCafe\.venv\Scripts\python.exe' .\tools\check_sqlite_runtime.py --require-fixed
```

위 명령은 저장소 루트에서 실행하며 기본 설치 경로의 서버 venv를 사용합니다. 사용자 지정 설치 경로이면 실행 파일 경로를 조정합니다. 점검 도구는 이 소스 저장소에 있으며 기존 v1.4.0 패키지에 자동 설치되지 않습니다. 개발 venv가 활성화된 경우 `python tools/check_sqlite_runtime.py --require-fixed`로 실행합니다. 확인되지 않은 엔진이면 종료 코드 1로 운영 승인 검사를 막습니다. 기존 설치 스크립트를 이 후보 때문에 임의로 변경하지 않습니다.

로컬 CPython 3.13.5는 SQLite 3.45.3, source ID `2024-04-15 13:34:05 8653b758870e6ef0c98d46b3ace27849054af85da891eb121e9aaa537f1e8355`로 수정 범위를 만족하지 않습니다. 이전 Windows 조사 3.12.10/3.13.16은 각각 SQLite 3.49.1/3.50.4였습니다. 새 Windows 검증에서도 런타임을 직접 출력합니다.

[공식 3.13.17 Windows 빌드 설정](https://github.com/python/cpython/blob/v3.13.17/PCbuild/python.props)은 여전히 sqlite-3.50.4.0을 지정합니다. 3.12/3.13과 현 설치 구조에서 검증된 수정 엔진 공급 경로를 확보하지 못했습니다. 독자 Windows 빌드·DLL 교체·다른 DB 모듈 주입은 하지 않습니다. 이 상태에서는 테스트가 통과해도 운영 적용 불가입니다. 버그가 이번 503의 원인이라는 증거도 없습니다.

## 검증 계획과 범위

기존 전체 Python/JS, Ubuntu/Windows 3.12/3.13 필수 CI를 유지합니다. 원본 test_load.py는 수정하지 않습니다. 추가 계측 검사는 동일 원본 함수를 실제 B 앱에서 다시 호출하며 native Lock만 감싸 대기/보유 평균·최대·p95·p99를 기록합니다. PR #3의 DB 어댑터/함수 계측은 복사하지 않습니다.

별도 경량 workflow는 Windows 각 버전에서 원본/계측 검사 및 경합·fault 검사를 2회씩 실행합니다. 요청·SQLite 15초/job 10분은 유지합니다. 테스트 성공과 엔진 안전성 판정을 분리합니다. 백업/복원·재시작·고객 쿠키/기존 DB·QR 보존은 기존 회귀 검사에 포함됩니다. 실제 카페 Windows 10 노트북·전원 차단·모바일 네트워크 검증은 미실시입니다.

검증 결과와 PR 링크는 실제 실행 완료 후 아래에 추가합니다.

## 학습 자료

[실제 핵심 코드와 따라 할 검사](../BACKEND_LEARNING.md). 구현은 AI 지원 작업이며 소유자의 이해·재현·운영 경험으로 자동 간주하지 않습니다.

## 구현 코드 83a5d35 검증 결과

- 로컬 전체 Python: 98 passed, 45.11초. MAISON_HTTP_TESTS=1 및 소켓 실행 권한을 부여해 실제 Waitress 재시작/복원 검사를 포함했습니다. 최초 권한 제한 실행의 2개 소켓 PermissionError는 앱 실패와 구분하고 허용 환경에서 재검증했습니다.
- JavaScript: 41 passed / 0 failed.
- [Windows 반복 실행 41073128683](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41073128683/attempts/1): 3.12/3.13 각각 2회, job마다 원본 비계측/동일 함수 계측 및 경합·정리 검사 15개 모두 통과. 매 부하 400고객/4000스탬프/400지급, 무결성과 중복 조건은 원본 검사가 확인합니다. 계측마다 획득/해제 5200회입니다. 취소 0건.
- [실제 엔진/source ID/통계 원본](../evidence/transaction-write-scope-windows.json): Windows 3.12.10은 SQLite 3.49.1, 3.13.16은 3.50.4로 수정 범위 미충족. 보유 평균은 사례별 5.911~8.368ms, 보유 p99는 19.142~25.579ms, 대기 최대 376.689~432.611ms였습니다. 이전 PR #3 A/B와 호스트·코드·응답 읽기 경계가 달라 수치를 직접 합치거나 성능 배수로 일반화하지 않습니다.

공식 setup-python 공급 목록에 3.13.17 x64도 있어 별도의 격리 Windows probe로 실제 엔진/source ID를 확인합니다. 기존 3.12/3.13 설치 구성은 바꾸지 않습니다. 이 probe의 결과는 완료 후 추가합니다.


## 공식 최신 패치 probe와 최종 판정

[공식 Python 3.13.17 격리 Windows 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41073836202/attempts/1)은 실제 SQLite 3.50.4 / source ID `2025-07-30 19:33:53 4d8adfb30e03f9cf27f800a2c1ba3c48fb4ca1b08b0f5ed59a4d5ecbf45e20a3`를 확인했습니다. [probe 원본](../evidence/official-python31317-runtime.json)을 보존합니다. 관측 source ID는 [공식 3.50.4 릴리스](https://sqlite.org/releaselog/3_50_4.html)와 일치하고, 3.12.10의 source ID는 [공식 3.49.1 릴리스](https://sqlite.org/releaselog/3_49_1.html)와 일치합니다. 해당 공식 릴리스는 WAL-reset 수정 이전입니다. 별도 vendor 백포트를 검증한 것은 아닙니다. 로컬 --require-fixed 검사는 실제로 종료 코드 1임을 확인했습니다.

구현 코드 83a5d35의 [전체 PR CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41073201327/attempts/1)와 [push CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41073128531/attempts/1)도 Ubuntu/Windows × 3.12/3.13, JS, PowerShell, 공개 검사에서 성공했습니다. 후속 커밋은 문서/근거/격리 probe만 추가하며 그 CI는 별도로 표시합니다.

추천은 이 B 구현 후보를 유지하되 **공식 수정 엔진 공급 및 그 런타임에서 재검증 전에는 운영 적용하지 않는 것**입니다. 요구 범위의 3.12/3.13/현 설치 체계에서 안전한 공급을 검증하지 못했으므로 독자 빌드·임의 DLL·타 모듈 우회로를 만들지 않습니다. 실제 카페 노트북 런타임은 아직 직접 확인하지 못했습니다. API 실패 후 이미 commit된 요청은 응답 전달 실패와 구별이 어려울 수 있으며 상품/지급 PK가 재요청의 중복을 막습니다. 합성 검사의 반복 통과가 실기기·전원 단절 안정성을 보증하지 않습니다.

PR #3은 조사 원본으로 그대로 Draft·미병합, PR #4도 운영 차단 사유 때문에 Draft·미병합입니다. main 보호·v1.4.0·운영 DB/설정은 유지합니다. 실제 운영 채택/릴리스/배포 결정은 후속 작업입니다.

### 후속: 공식 Python 3.15.2

위 차단 판정은 3.12/3.13 공급 확인 시점의 기록입니다. 이후 [DEV-008](DEV-008-python315-runtime.md)에서 공식 3.15.2의 실제 수정 엔진과 기존 lock/B 호환성을 별도로 검증합니다. 과거 관측값을 수정하거나 3.13에 수정이 포함됐다고 소급하지 않습니다.
