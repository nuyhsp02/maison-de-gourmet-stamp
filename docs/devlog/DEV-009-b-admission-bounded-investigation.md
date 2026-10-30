# DEV-009 · B 방식 간헐 admission timeout 한정 조사

기록일: 2026-10-31 (Asia/Seoul). 대상 [Draft PR #4](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/4). [DEV-006](DEV-006-windows-concurrency-investigation.md)~[008](DEV-008-python315-runtime.md)의 근거를 보존합니다. A/B 실험을 다시 만들지 않습니다.

## 기존 사실과 이번 조사 범위

[41083540047 / Windows 3.15.2](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41083540047/job/124158634903)은 수정 SQLite 3.53.4에서도 원본 400명/동시 30개 검사가 503으로 실패했습니다. 97 passed/1 failed, admission timeout 경고 7건, SQLite OperationalError 로그 0건입니다. 이 로그에는 대기 중 다른 요청의 진행/COMMIT/close 구간이 없어 느린 직렬 쓰기와 특정 요청의 장기 배제를 구분할 수 없습니다. 이후 같은 앱 코드의 성공 CI가 있어도 해결 근거로 바꾸지 않습니다.

[Python Lock 문서](https://docs.python.org/3.15/library/threading.html#lock-objects)는 대기 스레드 중 누가 다음에 획득하는지 정의하지 않습니다. 불공정성은 가능한 가설이며 이 사실 자체가 이번 실패의 원인 증명은 아닙니다. [SQLite WAL 문서](https://sqlite.org/wal.html#automatic_checkpoint)는 COMMIT/마지막 연결 close에서 자동 checkpoint가 생길 수 있음을 설명합니다. close와 COMMIT의 겹침만으로 내부 checkpoint나 OS 원인을 확정하지 않습니다.

## 제한된 계측 설계

운영 app.py, 스키마, 설정, 15초, 내구성, 원본 test_load.py를 바꾸지 않습니다. `tests/b_admission_probe.py`는 명시적으로 실행할 때만 쓰는 pytest 플러그인입니다. 기존 원본 비계측 함수는 그대로 실행하고, 기존 timed twin에만 native Lock/연결 관찰자를 추가합니다. SQL/매개변수/고객 ID/QR/쿠키/오류 문자열을 기록하지 않습니다.

- 대기·보유 평균/최대/p95/p99, 최대 대기자 수, 503/OperationalError 분류
- 느린 대기 중 다른 획득/나중 진입자 획득/성공 POST 응답 수와 보유·COMMIT·close 겹침
- BEGIN/쓰기 SQL/COMMIT/rollback/연결·초기 PRAGMA/close 구간 및 느린 보유 분해
- 원본과 계측 각각 종료 후 별도 읽기 연결로 고객/스탬프/지급, 중복, FK, 무결성, 지급-완성 관계 확인

성공이면 원본 함수의 400/4000/400과 모든 기존 조건을 그대로 요구합니다. 실패 후 부분 DB 검사는 성공 고객 400명 완료 주장과 구분합니다. 계측은 추가 Python 작업/짧은 내부 mutex로 스케줄링을 바꿀 수 있으므로 원본 비계측 결과를 우선 보존합니다. close 겹침 합계는 동시 연결 시간을 더하므로 실제 벽시계보다 클 수 있습니다. SQLite 내부 checkpoint를 직접 계측하거나 강제 checkpoint를 실행하지 않습니다.

추가 Windows **진단 실행 상한은 3회**, 우선 1회 실행합니다. `[b-probe]`가 포함된 push 커밋의 Windows 3.15.2 CI 한 작업만 opt-in하며, 일반 PR/다른 버전의 필수 검사는 원래 전체 검사 명령을 유지합니다. 자동 재시도는 없고 추가 진단 없이 생성되는 통상 필수 CI를 진단 반복으로 세지 않습니다. 진단 결과와 원본 PR CI 결과를 함께 기록합니다. 결과 확인 전 공정 큐나 추가 운영 변경은 하지 않습니다.

## 개선 후보의 최소 비용과 위험 (아직 구현하지 않음)

| 선택지 | 필요한 구현/검증 | 판단 조건과 위험 |
|---|---|---|
| 현 B 유지 | 운영 코드 변경 없음, 실패 안내·상태 재조회·수동 재시도 절차 확인 | 원인 미확정이면 기본값. 합성 실패를 해결로 표현하지 않음 |
| 임계 구간 추가 축소 | 재검증/쓰기/COMMIT/rollback을 밖으로 옮기지 않음 | 현재 안전 경계상 명백히 제거할 I/O가 확인되지 않음 |
| 공정한 admission | 작은 FIFO ticket/deque+Condition, timeout 취소/취소된 head 제거/예외 해제 테스트 필요 | 후발 요청의 반복 추월 근거가 있을 때만 후보. 느린 COMMIT 총량은 해결하지 못하고 head-of-line 지연·교착 위험 추가 |
| 별도 writer worker | 큐/수명/종료/예외/결과 전달까지 추가 검증 | 현재 한정 조사에 과도함. 필요성 확인 전 도입하지 않음 |

시간 제한 증가·모든 POST 자동 재시도·synchronous 완화는 후보에서 제외합니다.

## 운영 규모와 수용 기준 제안

합성 검사는 400명 각각 새 세션 1+적립 11(중복 1 포함)+지급 1로 총 5,200회 POST를 30개 작업에서 지체 없이 보냅니다. 실제 카페의 동시에 열린 고객 화면 30개와 같은 요청량이 아닙니다. 이 검사는 그대로 정합성/최악 부하 회귀로 보존합니다.

별도 현장 인수에서는 실제 노트북에서 평상시·짧은 주문 집중 시 요청률, 응답 p95/p99/최대, 503/429 및 수동 재시도 후 중복/유실 여부를 측정하고 허용 기준을 운영자와 정할 것을 제안합니다. 임의 고객/초당 수치나 운영 성과를 만들지 않습니다. 현재 앱은 오류 문구를 보이고 일반 동작 실패 시 상태를 다시 읽으며, 스캐너는 503에 중지해 요청 폭주를 피합니다. 적립·지급은 현재 상태 확인 후 사용자가 한 번 수동 재시도하고, 신규 카드/복구 링크 회전은 무조건 재전송하지 않는 방식이 안전합니다. 이는 정책 제안이며 이번 작업에서 UX/운영 기준을 변경하지 않습니다.

## 결과

추가 Windows 진단 실행 전. 원인은 미확정이며 PR #4는 Draft·미병합, 운영 승인 보류입니다. main·PR #3·v1.4.0·실제 DB/설정·배포는 변경하지 않습니다.
