# B 방식과 멱등성으로 배우는 백엔드

AI가 구현한 코드를 읽고 직접 검증하기 위한 자료입니다. 구현 이력은 [DEV-007](devlog/DEV-007-transaction-write-scope.md), 런타임/잔여 위험은 [DEV-008](devlog/DEV-008-python315-runtime.md)·[009](devlog/DEV-009-b-admission-bounded-investigation.md), 경험 기록은 [PORTFOLIO_RECORDS](PORTFOLIO_RECORDS.md)를 함께 봅니다. 현재 시험 코드는 **7667f5c**이며 문서 후속 커밋 때문에 기존 후보 ZIP을 재빌드하지 않습니다. PR #4는 Draft입니다.

## 핵심 코드 세 곳과 읽는 순서

1. [schema.sql: stamps와 reward_audit](../app/schema.sql#L19): `(customer_id, product_id)` 복합 PRIMARY KEY와 `reward_audit.customer_id` PRIMARY KEY를 찾습니다. 누가 무엇을 한 번만 할 수 있는지 먼저 적어 봅니다.
2. [app.py: collect](../app/app.py#L372) → [redeem](../app/app.py#L431) → [write_transaction](../app/app.py#L159): INSERT 결과, 이미 지급된 분기, 두 지급 쓰기의 COMMIT 경계를 읽습니다. [test_write_scope의 적립/지급 경합](../app/tests/test_write_scope.py#L164)이 어떤 순서를 허용하는지 연결합니다.
3. [field_http.py: response_loss](../tools/field_http.py#L285): app 200/DB COMMIT 확인 → 미수신 확인 → 고객 소켓 종료 → GET → 지급 POST 재요청 → 감사 기록 1개 검사를 따라갑니다. 이 코드는 실제 Waitress/TCP를 쓰는 시험 도구이며 운영 API에 장애 기능을 넣지 않습니다.

## A. 멱등성: 같은 응답보다 같은 업무 효과

같은 논리 요청을 반복해도 의도한 최종 업무 상태가 한 번 처리한 상태와 같으면 멱등적입니다. [HTTP 의미 명세](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)의 개념과 연결되지만, **POST 자체가 자동으로 멱등적인 것은 아닙니다.** 이 앱은 특정 업무의 DB 키와 분기로 중복 효과를 막습니다.

예를 들어 같은 고객이 같은 상품을 두 번 적립하면 첫 응답은 `added=true`, 둘째는 `added=false`입니다. 응답이 달라도 스탬프는 1개이므로 적립 효과는 같습니다. 로그·rate-limit 카운터·HTTP 응답·DB 파일 바이트까지 모두 동일해야 한다는 뜻은 아닙니다.

## B. 실제 적립과 지급 처리

| 구간 | 코드에서 확인할 것 | 반복 결과 |
|---|---|---|
| collect의 검증 | 세션/CSRF·기간·QR 확인, 잠금 안 세션/기간 재검사 | 인증 실패/기간 종료/요청 제한이면 401·403·409·429 등이 가능. 모든 재요청이 200이라는 보장은 아님 |
| `INSERT OR IGNORE` | 검증한 고객/상품을 INSERT, `cursor.rowcount == 1`로 added 결정 | 새 조합이면 true, 같은 조합이면 복합 PK 충돌로 무시되어 false. 최초 collected_at도 보존 |
| redeem의 `redeemed_at` 조회 | 잠금/트랜잭션 안에서 이미 지급됐는지 다시 확인 | 최초 정상 지급은 `already_redeemed=false`, 이미 지급된 재요청은 true |
| 최초 지급의 두 쓰기 | `customers.redeemed_at` UPDATE와 `reward_audit` INSERT를 한 트랜잭션에 둠 | COMMIT으로 함께 확정, 확정 전 쓰기 오류는 rollback. 이후 HTTP 실패까지 확정을 취소한다는 뜻은 아님. 재요청에서 이미 지급된 분기는 둘 다 다시 쓰지 않음 |

[SQLite IGNORE](https://sqlite.org/lang_conflict.html)는 이 경로에서 중복 행을 건너뛰지만 모든 DB 오류를 성공으로 바꾸는 기능은 아닙니다. 예를 들어 외래키 위반은 무시하지 않습니다. `reward_audit` PK는 고객별 감사 행을 최대 1개로 제한하지만, **10종 완성 및 고객 지급 시각과 감사 행의 일치까지 PK만으로 보장하지는 않습니다.** 코드의 조건 검사·원자적 쓰기와 테스트가 함께 필요합니다.

여기서 같은 고객은 **동일 DB 고객 레코드**입니다. 다른 브라우저가 복구 없이 새 카드를 만들면 별도 고객입니다. 실제 사람 한 명에게 평생/여러 카드 전체에서 한 번만 지급한다는 인증은 없습니다. 유효한 세션·CSRF와 정책 조건 안에서 보장하는 업무 효과를, 모든 상황의 공통 자동 재시도 보장으로 넓혀 말하지 않습니다.

## C. 멱등성·트랜잭션·정합성·동시성은 다르다

| 개념 | 묻는 질문 | 이 프로젝트의 역할 |
|---|---|---|
| 멱등성 | 같은 업무를 다시 요청해도 추가 효과가 없는가? | 같은 고객/상품 PK, 이미 지급됨 분기 |
| 원자성 | 관련된 쓰기가 전부 확정되거나 전부 취소되는가? | 지급 시각과 감사 INSERT의 한 COMMIT/ROLLBACK |
| 정합성 | 데이터가 업무 규칙과 제약을 만족하는가? | FK/PK, 10종 검사, 지급 상태·감사 일치 검사 |
| 동시성 제어 | 요청이 겹쳐도 안전한 판단/쓰기 순서를 유지하는가? | Python mutex, BEGIN IMMEDIATE, 잠금 안 재검증 |

[SQLite 트랜잭션](https://sqlite.org/lang_transaction.html)은 원자적 확정의 경계입니다. 트랜잭션을 썼다는 이유만으로 멱등적이지 않습니다. 쿠키 없는 `/api/session`은 호출마다 고객+세션을 원자적으로 새로 만들 수 있습니다. 반대로 지급 쓰기를 두 COMMIT으로 나눈다는 **가정**에서는 중간 실패로 한쪽만 남을 수 있으므로, 중복 분기만으로 정합성을 지킬 수 없습니다. 실제 코드는 한 트랜잭션입니다.

B 방식의 기본 개념도 함께 기억합니다.

- **Race condition:** 사전 세션 검사 후 기다리는 동안 세션 폐기/이벤트 종료가 가능해 잠금 안에서 다시 검사합니다.
- **Mutex / critical section:** 한 Waitress 프로세스의 쓰기 구간을 조정합니다. 다른 프로세스까지 막는 전역 잠금은 아닙니다.
- **BEGIN IMMEDIATE:** SQLite 쓰기 트랜잭션을 시작합니다. Python mutex와 다르며 외부 연결의 경합/오류도 가능합니다.
- **COMMIT / ROLLBACK:** 성공 또는 취소를 확정한 뒤 잠금을 풉니다. 정리 실패 경로는 test_write_scope의 fault 검사에서 확인합니다.
- **close / WAL checkpoint:** 정상 연결 종료와 응답 snapshot은 잠금 밖입니다. close/COMMIT 지연과 수정 엔진 확인은 별도 문제입니다.
- **원자성과 정합성:** 위 표처럼 관련 쓰기 경계와 업무 규칙을 각각 설명할 수 있어야 합니다.
- **처리량과 지연:** 일정 시간의 완료 건수와 개별 요청의 p95/p99는 다릅니다. 합성 검사의 수치를 실사용 성과로 쓰지 않습니다.

## D. 반복 호출을 자동으로 안전하다고 볼 수 없는 API

| API | 실제 분기와 반복의 의미 |
|---|---|
| `/api/session` | 유효한 기존 쿠키/CSRF가 있으면 기존 snapshot을 반환. **쿠키가 없거나 만료됐다면** 새 고객/세션을 생성. 첫 응답의 Set-Cookie를 잃은 상태에서 재요청하면 다른 고객이 생길 수 있어 전체 API를 무조건 멱등적이라 부를 수 없음 |
| `/api/recovery-link` | 매 호출 새 토큰·만료 시각을 만들고 같은 고객 행을 UPSERT. 행은 1개여도 값/유효 링크가 바뀌고 이전 링크는 무효. 따라서 같은 행 수가 유지된다는 이유로 멱등적이지 않음 |
| `/api/recover` | 같은 고객 기록을 복구하지만 현재 세션을 바꾸고 새 쿠키를 발급. 고객을 중복 생성하지 않는 것과 전체 세션 상태가 불변인 것은 다름 |

따라서 모든 POST를 같은 정책으로 자동 재시도하지 않습니다. 특히 새 카드 생성·복구 링크 회전을 오류 해결용으로 반복하지 않습니다. 적립/지급도 먼저 GET으로 현재 상태를 확인하고, 세션이 유효한지 확인한 뒤 필요한 경우만 수동 재시도합니다.

## E. COMMIT 뒤 응답이 사라지면 누가 무엇을 아는가?

1. 고객이 지급 확인 POST를 보냅니다. 서버는 세션·완성 여부 등을 검증합니다.
2. 서버가 지급 시각과 감사 행을 COMMIT합니다. **DB에는 지급 완료가 기록돼 있습니다.**
3. 응답이 유실되면 고객은 성공 응답을 못 봅니다. 고객에게는 '성공인지 모름'이지 'DB 실패 확정'이 아닙니다.
4. 같은 고객으로 GET `/api/state`를 보내면 서버의 `redeemed` 상태를 확인할 수 있습니다. 이미 완료면 새 지급 요청은 필요하지 않습니다.
5. 유효한 동일 고객이 지급 POST를 다시 보내도 이미 지급된 분기로 응답하며 감사 행을 늘리지 않습니다. 도구는 이 재요청과 DB 1개를 직접 확인합니다. Timeout을 무조건 응답 유실 성공으로 인정하지 않습니다.
6. **DB 지급 1회와 실물 선물 전달 1회는 자동으로 같지 않습니다.** 직원 인증이 없어서 고객이 임의로 누를 수도 있고, 실물 전달은 사람이 확인해야 합니다. 예전 백업 복원으로 지급 기록이 사라진 경우에도 실물 전달은 되돌아가지 않아 별도 대조가 필요합니다.

의도한 admission 503은 잠금 획득 실패로 BEGIN 전 중단되지만, 고객이 보는 일반 503만으로 그 원인이나 COMMIT 여부를 단정하지 않습니다. 실제 오류 후에는 상태 재조회가 먼저입니다. 과거 간헐 admission timeout의 근본 원인은 여전히 미확정입니다.

## 직접 실행할 기존 테스트 세 개

Windows의 [FIELD_SETUP](FIELD_SETUP.md)에서 `$package`(압축 해제 폴더), `$basePython`(검증한 3.15.2)를 설정한 같은 창에서 실행합니다. 학습에는 pytest가 필요하므로 **운영/시험 서버 venv를 바꾸지 않고** 별도 학습 venv를 사용합니다. 아래 준비는 처음 한 번이며 기존 폴더를 덮어쓰지 않습니다.

```powershell
$studyVenv = 'C:\MaisonFieldStudy\venv-7667'
if (Test-Path $studyVenv) { throw '기존 학습 venv는 보존하고 확인 후 재사용하거나 새 경로를 정하세요.' }
& $basePython -m venv $studyVenv
$studyPython = "$studyVenv\Scripts\python.exe"
& $studyPython -m pip install --only-binary=:all: -r "$package\requirements-test.lock"
Set-Location -LiteralPath $package
$env:MAISON_HTTP_TESTS = '1'
```

한 명령씩 실행하고 종료 코드를 확인합니다. 이미 검증한 개발 venv가 있는 macOS/Linux에서는 아래 `& $studyPython` 대신 그 Python 경로를 쓰고 `MAISON_HTTP_TESTS=1`로 설정합니다. 실제 운영 DB를 인자로 지정하지 않습니다. 세 테스트 모두 임시 DB를 사용합니다.

```powershell
& $studyPython -m pytest -q app/tests/test_collection.py::test_parallel_duplicate_collect_and_redeem
& $studyPython -m pytest -q app/tests/test_write_scope.py::test_collect_and_redeem_collision_keeps_single_reward
& $studyPython -m pytest -q tests/test_field_http.py::test_reply_loss_real_waitress_commit_requery_and_thread_cleanup
```

| 테스트 | 예상 결과와 확인 범위 |
|---|---|
| 첫째 | 1 passed. 동일 고객의 30개 적립 요청 중 added=true 합계 1, 30개 지급 요청 중 최초 처리 합계 1. Flask 테스트 클라이언트이며 실물 고객 30명/실서비스 성능 검사가 아님 |
| 둘째 | 1 passed. 마지막 적립과 지급이 경합할 때 지급의 200/409는 기존 순서에 따라 허용. 후속 처리 후 스탬프 10개·감사 1개. Flask 테스트 클라이언트의 순서/정합성 검증 |
| 셋째 | 1 passed, **skipped는 완료 아님**. 실제 Waitress/TCP로 COMMIT 후 응답 유실·GET·재지급 없음·서버 스레드/소켓 정리 확인. HTTPS/휴대폰/Windows 10 인수를 대신하지 않음 |

기존 전체 회귀·원본 400명/동시 30개 검사는 그대로 유지합니다. 이번 학습 때문에 부하 강도를 바꾸거나 추가 동시성 연구를 하지 않습니다.

## 스스로 설명할 질문 다섯 개

1. 첫 collect가 added=true, 다음이 false인데도 왜 업무 효과가 멱등적인가?
2. stamps 복합 PK와 INSERT OR IGNORE는 각각 무엇을 하고, FK 오류도 무시하는가?
3. 트랜잭션이 있는 session 생성과 UPSERT인 recovery-link가 왜 항상 멱등적이지 않은가?
4. 지급 COMMIT 후 응답 유실 때 서버와 고객이 아는 사실은 어떻게 다르며, 왜 GET부터 하는가?
5. DB 감사 행 1개가 실물 선물 1회나 실제 사람 전체의 중복 방지를 증명하지 못하는 이유는 무엇인가?

## 면접에서 1분 안에 설명할 예시

아래는 **코드를 이해한 뒤 본인 말로 바꿀 설명 예시**입니다. 직접 실행하지 않은 테스트/Windows 운영 경험을 덧붙이지 않습니다.

> 이 앱은 같은 고객이 같은 상품을 다시 요청해도 스탬프가 늘지 않도록 고객과 상품의 복합 기본키와 INSERT OR IGNORE를 사용합니다. 첫 응답과 중복 응답은 다르지만 최종 스탬프는 하나입니다. 보상은 지급 시각과 감사 기록을 한 트랜잭션으로 확정하고, 재요청에는 이미 지급된 상태를 돌려줍니다. 그래서 COMMIT 후 응답이 사라져도 현재 상태를 조회하고 중복 기록을 막을 수 있습니다. 다만 쿠키 없는 새 카드 생성이나 복구 링크 회전까지 같은 성질은 아닙니다. 멱등성은 반복 효과, 원자성은 관련 쓰기의 확정 범위라는 차이가 있습니다. 코드와 자동 테스트의 근거를 설명하되 실제 선물 전달이나 실장비 운영 성과까지 검증됐다고 말하지 않겠습니다.

## AI 지원과 직접 검증 경험의 구분

이 대화에서 AI 도구가 macOS로 실행한 학습 테스트 3개는 **3 passed / 1.12초**이며, Git 없는 후보 ZIP 압축 해제 폴더에서도 같은 세 검사가 **3 passed / 0.75초**였습니다. 사용자가 직접 실행한 Windows 결과가 아닙니다. 후보 검증/HTTP 검사와 실장비 미실행 상태는 [FIELD_SETUP](FIELD_SETUP.md)에서 구분합니다. 사용자는 직접 실행한 환경·명령·예상/실제 차이·실패·이해한 이유를 [기존 로그 양식](devlog/TEMPLATE.md)에 남기세요. AI가 구현하고 검수한 코드를 보았다는 것과 본인이 오류를 재현·분석·운영했다는 경험은 다릅니다.
