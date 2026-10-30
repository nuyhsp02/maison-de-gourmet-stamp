# DEV-007 · 트랜잭션 중심 쓰기 잠금 구현 후보

상태: main cceed35 기준 별도 브랜치 `fix/transaction-write-scope`. 운영 채택 보류 / Draft PR 예정. 릴리스·현장 배포 없음.

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
.\.venv\Scripts\python.exe .\app\tools\check_sqlite_runtime.py --require-fixed
```

위 경로는 저장소 전체를 `app`에 둔 Windows 배포 구조 기준입니다. 저장소 루트에서 직접 실행할 때는 `python tools/check_sqlite_runtime.py --require-fixed`입니다. 확인되지 않은 엔진이면 종료 코드 1로 운영 승인 검사를 막습니다. 기존 설치 스크립트를 이 후보 때문에 임의로 변경하지 않습니다.

로컬 CPython 3.13.5는 SQLite 3.45.3, source ID `2024-04-15 13:34:05 8653b758870e6ef0c98d46b3ace27849054af85da891eb121e9aaa537f1e8355`로 수정 범위를 만족하지 않습니다. 이전 Windows 조사 3.12.10/3.13.16은 각각 SQLite 3.49.1/3.50.4였습니다. 새 Windows 검증에서도 런타임을 직접 출력합니다.

[공식 3.13.17 Windows 빌드 설정](https://github.com/python/cpython/blob/v3.13.17/PCbuild/python.props)은 여전히 sqlite-3.50.4.0을 지정합니다. 3.12/3.13과 현 설치 구조에서 검증된 수정 엔진 공급 경로를 확보하지 못했습니다. 독자 Windows 빌드·DLL 교체·다른 DB 모듈 주입은 하지 않습니다. 이 상태에서는 테스트가 통과해도 운영 적용 불가입니다. 버그가 이번 503의 원인이라는 증거도 없습니다.

## 검증 계획과 범위

기존 전체 Python/JS, Ubuntu/Windows 3.12/3.13 필수 CI를 유지합니다. 원본 test_load.py는 수정하지 않습니다. 추가 계측 검사는 동일 원본 함수를 실제 B 앱에서 다시 호출하며 native Lock만 감싸 대기/보유 평균·최대·p95·p99를 기록합니다. PR #3의 DB 어댑터/함수 계측은 복사하지 않습니다.

별도 경량 workflow는 Windows 각 버전에서 원본/계측 검사 및 경합·fault 검사를 2회씩 실행합니다. 요청·SQLite 15초/job 10분은 유지합니다. 테스트 성공과 엔진 안전성 판정을 분리합니다. 백업/복원·재시작·고객 쿠키/기존 DB·QR 보존은 기존 회귀 검사에 포함됩니다. 실제 카페 Windows 10 노트북·전원 차단·모바일 네트워크 검증은 미실시입니다.

검증 결과와 PR 링크는 실제 실행 완료 후 아래에 추가합니다.

## 학습 자료

[실제 핵심 코드와 따라 할 검사](../BACKEND_LEARNING.md). 구현은 AI 지원 작업이며 소유자의 이해·재현·운영 경험으로 자동 간주하지 않습니다.
