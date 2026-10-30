# B 방식으로 배우는 백엔드

이 문서는 AI가 구현한 후보 코드를 직접 읽고 검증하기 위한 안내입니다. [변경/검증 기록](devlog/DEV-007-transaction-write-scope.md)과 [기존 기록 원칙](PORTFOLIO_RECORDS.md)을 함께 읽습니다. [현재 런타임/운영 승인 상태](devlog/DEV-008-python315-runtime.md)는 후속 검증 기록을 확인합니다.

## 핵심 코드 세 곳과 읽는 순서

1. [app.py의 write_transaction](../app/app.py): acquire(15초), BEGIN, yield, commit, except/rollback, finally/release 순으로 읽습니다. A는 before_request에서 전체 요청을 잠갔고 B는 여기서만 잠급니다.
2. 같은 파일의 collect → recover → redeem: 잠금 밖 사전 검사와 잠금 안 재검사를 비교합니다. recovery-link도 이제 명시적 트랜잭션입니다. snapshot은 별도 읽기 트랜잭션입니다.
3. [test_write_scope.py](../app/tests/test_write_scope.py): 조건 변경 경합 → commit/rollback/close fault → snapshot 경합 → 원본 부하 계측 순으로 읽습니다. [schema.sql](../app/schema.sql)의 PK/FK를 함께 확인합니다.

## 일곱 개념을 실제 코드에 연결하기

- **동시성과 race condition:** 세션을 검사한 뒤 잠금을 기다리는 동안 다른 요청이 복구하며 세션을 삭제할 수 있습니다. collect의 require_identity 재검사와 강제로 순서를 제어하는 경합 테스트가 이를 다룹니다. 이벤트 기간도 기다리는 사이 끝날 수 있습니다.
- **Mutex와 critical section:** app.write_lock은 한 프로세스의 쓰기 구간을 직렬화합니다. SQLite 전체나 다른 프로세스를 보호하는 전역 잠금은 아닙니다. 잠금을 적게 잡으면 대기 누적이 줄지만, 필요한 재검사를 빼면 안전하지 않습니다.
- **BEGIN IMMEDIATE:** SQLite 쓰기 예약을 트랜잭션 시작 때 얻습니다. Python mutex와 역할이 다르며 외부 writer 때문에 SQLite 자체에서 실패할 수도 있습니다. busy_timeout은 기존 15초입니다.
- **COMMIT/ROLLBACK:** commit은 변경 전체를 확정하고 rollback은 미확정 변경을 취소합니다. commit 실패 시 잠금을 바로 풀지 않고 rollback/필요한 close까지 수행합니다. close까지 실패하면 서버 재시작 전 쓰기를 차단합니다.
- **Connection close와 WAL checkpoint:** close는 단순 객체 정리 이상의 지연을 가질 수 있습니다. 마지막 연결 정리에는 WAL checkpoint가 포함될 수 있습니다. B의 정상 close는 쓰기 잠금 밖입니다. 이 동시성이 안전한 엔진인지 별도 확인해야 합니다.
- **원자성과 정합성:** 고객 지급 시각과 reward_audit INSERT가 같이 commit되어야 합니다. stamps 복합 PK와 reward_audit PK가 각각 중복 적립/지급을 막습니다. 잠금과 DB 제약을 함께 사용합니다. snapshot의 여러 조회는 같은 읽기 시점을 사용합니다.
- **처리량과 지연:** POST/s는 일정 시간의 처리 건수이고 p95/p99는 개별 요청 대기의 꼬리입니다. 평균이 작아도 15초 실패가 생길 수 있습니다. 부분 실패 실행을 400명 완주 성능과 비교하면 안 됩니다. 합성 Flask 클라이언트 성능은 실서비스 실적이 아닙니다.

## 직접 따라 하기 (저장소 루트)

기존 개발용 가상환경에서 실행합니다. 아래 Python은 Windows에서는 `.venv\Scripts\python.exe`로 바꿀 수 있습니다. 운영 DB를 지정하지 마세요. 테스트는 tmp_path의 별도 DB를 사용합니다.

```text
python tools/check_sqlite_runtime.py
python tools/check_sqlite_runtime.py --require-fixed
python -m pytest -q app/tests/test_write_scope.py
python -m pytest -q app/tests tests
node --test app/tests/scanner.test.cjs app/tests/app-flow.test.cjs
```

첫 명령의 SQLite 버전/source ID를 공식 자료와 대조합니다. 둘째는 미확인 엔진이면 코드 1이 예상됩니다. 이는 테스트 실패와 다른 운영 차단 조건입니다. Windows PowerShell에서 `$env:MAISON_HTTP_TESTS='1'`을 먼저 설정하면 전체 검사에 실제 loopback 서버 재시작 검사도 포함됩니다(macOS/Linux는 `MAISON_HTTP_TESTS=1 python -m pytest ...`).

원본 부하 테스트는 성공 시 고객 400/스탬프 4000/지급 400, 중복 적립 거절 및 integrity_check를 검사합니다. 계측 검사의 B_LOCK_TIMINGS를 보려면 `python -m pytest -q -s app/tests/test_write_scope.py::test_original_load_with_lock_timings`를 실행합니다. 건너뛴 HTTP 검사를 완료로 세지 마세요. 정확한 최종 테스트 수와 Windows 결과는 DEV-007을 확인합니다.

## 스스로 설명할 질문 다섯 개

1. 세션 검사에 성공했는데 잠금 안에서 왜 다시 검사하는가?
2. Python mutex와 BEGIN IMMEDIATE는 각각 누구의 경합을 막는가?
3. commit이 실패한 직후 잠금을 해제하면 어떤 문제가 생기는가?
4. 지급 timestamp와 감사 INSERT를 별도 commit하면 어떤 불일치가 가능한가?
5. B가 테스트에서 빨라도 운영 승인 전 SQLite 엔진과 source ID를 확인해야 하는 이유는 무엇인가?

AI가 구현·실행한 결과를 읽는 것과 직접 검증한 경험을 구분합니다. 사용자는 직접 명령을 실행한 환경/결과, 실패를 재현한 순서, 이해한 이유를 [개발 로그 템플릿](devlog/TEMPLATE.md)에 기록하세요. 아직 하지 않은 Windows 10 운영·전원 차단 시험이나 실고객 성과는 쓰지 않습니다.
