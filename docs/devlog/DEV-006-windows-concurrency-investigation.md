# DEV-006 · 병합 후 Windows 동시성 503 조사

- 상태: **Windows에서 native Lock의 admission timeout을 재현. 장시간 보유/대기 순서 중 근본 원인은 아직 미확정이며 해결 완료로 판정하지 않음.**
- 발생일: 2026-10-30, [main cceed35의 첫 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41038845367/attempts/1).
- 실행 주체: GitHub hosted runner와 AI 지원 코드/로그 대조·통제 실험. 실제 Windows 10 카페 서버의 재현/배포가 아님.
- 조사 PR: [#3](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/3), 브랜치 fix/windows-concurrency.

## 1. 문제와 예상 동작

PR #2는 성공한 CI를 거쳐 병합됐지만 main의 Windows/Python 3.13에서 `test_400_visitors_30_concurrent`가 `/api/collect`의 503 `storage_unavailable`로 실패했습니다. 결과는 83 passed / 1 failed입니다. 기대는 기존 400명·동시 30개 애플리케이션 클라이언트와 모든 중복/수집/지급/무결성 검사의 통과입니다. 이 테스트는 실사용 고객이나 외부 HTTP 성능 실적이 아닙니다.

## 2. 확인한 사실과 남은 가설

[성공 PR 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41038220983)과 실패 main 실행을 대조했습니다.

| 항목 | 성공 PR / 실패 main |
|---|---|
| 소스 | b11782f / cceed35, 앱·테스트·설정·의존성·CI 코드 차이 없음 |
| Runner agent | 둘 다 2.338.0 |
| Windows 이미지 | 둘 다 windows-2025-vs2026 / 20261021.278.1 |
| Python | 둘 다 3.13.16 x64 |
| 설치 패키지 | 로그에 기록된 고정 버전 목록 일치 |
| 전체 pytest 경과 | 성공 150.79초 / 실패 337.35초 |

이 시간 차이는 전체 CI 시험의 관측값이며 처리 성능 측정/원인 설명이 아닙니다. 실제 호스트의 CPU 스케줄링·디스크 지연은 비교 계측하지 않았습니다. 원래 두 실행은 SQLite 런타임 버전/분기별 대기 수치를 남기지 않았으므로 동일했다고 소급 단정하지 않습니다.

현재 503은 두 경로에서 같은 공개 JSON을 반환합니다.

1. Python `app.write_lock.acquire(timeout=15)` 실패.
2. SQLite `OperationalError` 처리.

첫 실패 로그에는 기존 SQLite 오류 로그가 없었습니다. 이는 1번과 양립하지만, 별도 분기 표식이 없어 **그 역사적 요청을 확정 분류하는 직접 증거로 취급하지 않습니다**. 잠금 경합/스케줄링, 긴 보유/IO 지연은 가설입니다. [Python Lock 문서](https://docs.python.org/3.13/library/threading.html#lock-objects)가 대기자 선택 순서를 보장하지 않는다는 사실만으로 이 사건을 starvation으로 확정할 수는 없습니다.

## 3. 조사와 통제 실험

먼저 원래 native Lock·15초·SQLite 설정·400/30 테스트를 그대로 유지한 [진단 커밋 3c6925d](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/3c6925dda889ea8525abbc427dda30b3d7e26d17)을 만들었습니다. 테스트에서만 획득/해제·대기/보유 시간·대기 중 다른 획득 횟수·종료 전 in_transaction·SQLite 오류 수를 집계했습니다.

프로세스 잠금 획득 후에는 g에 소유 여부를 기록하고, app context 종료에서 DB 연결 정리 후 finally로 해제합니다. 획득 실패 시에는 소유 표시가 없어 해제하지 않습니다. 적립/지급의 트랜잭션과 rollback 경로도 검토했으며 이를 임의로 변경하지 않았습니다. `in_transaction == false`만으로 읽기 커서/모든 SQLite 락의 부재까지 증명하지는 않습니다.

별도 통제 검사에서는 실제 슬롯을 계속 보유해 원래 15초를 소진시키고, 별도 SQLite 연결의 BEGIN IMMEDIATE로 SQLite 대기를 유발합니다. 두 경우 같은 503이지만 새 정적 admission 로그와 기존 SQLite 오류 로그로 구별되는지, 원인을 해제한 뒤 정상 적립되는지 확인합니다. **이는 원래 main 사건의 재현/원인 확정이 아니라 두 실패 경로의 재현입니다.**

## 4. 변경 범위와 보류한 수정

- [app.py](../../app/app.py): admission timeout에 정적 분류 로그 1개 추가. 공개 상태/JSON·15초·잠금 정책·SQL·스키마/보상 정책 변경 없음.
- [concurrency_support.py](../../app/tests/concurrency_support.py), [conftest.py](../../app/tests/conftest.py): 원래 부하 테스트에만 관찰자를 붙여 안전한 집계를 터미널 요약에 기록.
- [test_storage_diagnostics.py](../../app/tests/test_storage_diagnostics.py): 두 분기 구분·민감 오류 메시지 비노출·실제 제한/DB 경합 후 회복 검사.
- [test_load_native.py](../../app/tests/test_load_native.py)는 같은 원본 함수를 native Lock 그대로 한 번 더 호출합니다. 이 경로에는 타이밍/잠금 관찰자, 추가 teardown probe, sleep/재시도가 없습니다. 관찰에 의한 스케줄링 변화와 분리해 검사합니다.
- [test_load.py](../../app/tests/test_load.py)는 변경하지 않음. 400명·30개·5200회 변경 요청과 최종 고객/스탬프/지급/중복/무결성 검사를 유지.

원인 근거가 부족해 FIFO 교체, timeout 확대, SQLite 동기화/내구성 변경, 자동 재시도로 실패를 숨기는 수정은 하지 않았습니다. 관찰자 자체도 스케줄링에 영향을 줄 수 있어 통과를 해결 증거로 과장하지 않습니다.

## 5. 관측된 검증 결과

진단 전용 동일 커밋의 [push 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41041893004)과 [PR 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41042069631)은 Windows 3.12/3.13 각각 86개 통과였습니다.

| 환경 / 실행 | 최대 대기 ms | 최대 보유 ms | 대기 중 다른 획득 최대 | admission / SQLite 오류 |
|---|---:|---:|---:|---|
| 3.12 push | 875.00 | 219.00 | 29 | 0 / 0 |
| 3.12 PR | 1235.00 | 188.00 | 29 | 0 / 0 |
| 3.13 push | 2716.80 | 1163.33 | 29 | 0 / 0 |
| 3.13 PR | 2053.14 | 350.93 | 29 | 0 / 0 |

모두 획득/해제 5200/5200, 잔여 대기 0, 종료 전 열린 트랜잭션 관측 0입니다. 새로 기록된 SQLite는 3.12.10에서 3.49.1, 3.13.16에서 3.50.4였습니다. 이 관측 범위에서는 지속적인 슬롯 누수나 SQLite 오류 증거를 찾지 못했습니다.

두 실제 실패 경로 통제 검사는 로컬에서 원래 15초 설정을 유지해 통과했습니다. 최종 코드의 추가 Windows 반복 결과와 원래 main의 수정 없는 재실행은 아래 후속 검증 기록에 구분합니다. 이전 86개 결과에 새 통제 검사까지 포함됐다고 쓰지 않습니다.

### 원래 main의 수정 없는 재실행

[같은 main 실행의 attempt 2](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41038845367/attempts/2)에서 Windows 3.13 작업이 통과했습니다. 커밋 cceed35와 원래 테스트는 바꾸지 않았습니다. 첫 실패는 attempt 1에 보존돼 있습니다. 이것은 간헐성을 보여 주며 어떤 수정이 해결했다는 증거가 아닙니다.

### 비계측 원본 부하에서 재현된 실패 · 6909495

macOS/Python 3.13.5의 전체 회귀는 89개 통과(70.76초)였습니다. 기존 84개에 분기/비밀 비노출/두 실제 대기 경로 4개와 관찰자 없는 원본 부하 호출 1개를 추가했습니다.

그러나 같은 [6909495](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/690949552ae72af66dd60558b00a67f2a9ed4338)의 [push 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41052826136/attempts/1)은 Windows 3.12의 비계측 검사에서 **1 failed / 88 passed, 530.79초**였습니다. 비계측 부하 요약은 admission timeout 로그 **100건**, SQLite OperationalError **0건**입니다. 원본 테스트의 모든 검사와 native Lock을 유지한 상태에서 새 실패의 503 분기를 직접 구분했습니다. 이 사실로 원래 cceed35의 과거 요청까지 확정하지는 않습니다.

| 6909495 실행 / 환경 | 계측 최대 대기/보유 ms | 계측 중 다른 획득 최대 | 비계측 admission / SQLite | 전체 결과 |
|---|---:|---:|---|---|
| push / 3.12 | 7969.00 / 2250.00 | 29 | 100 / 0 | 1 failed, 88 passed |
| push / 3.13 | 7381.52 / 1264.84 | 29 | 0 / 0 | 통과 |
| PR attempt 1 / 3.12 | 1657.00 / 282.00 | 58 | 0 / 0 | 89 passed, 363.18초 |
| PR attempt 1 / 3.13 | 1898.92 / 324.88 | 29 | 0 / 0 | 89 passed, 334.60초 |
| PR attempt 2 / 3.12 | 1313.00 / 266.00 | 29 | 20 / 0 | 1 failed, 88 passed, 383.03초 |
| PR attempt 2 / 3.13 | 11738.21 / 1998.93 | 29 | 종료까지 관측 1 / 0 | CI 10분 제한으로 취소, 완료 검사 아님 |

[PR attempt 1](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41052835100/attempts/1)의 성공으로 push 실패를 해결됐다고 취급하지 않습니다. 계측 부하 자체는 네 작업에서 모두 획득/해제 5200/5200, admission/SQLite 오류 0, 종료 전 열린 트랜잭션 관측 0입니다. 실패한 비계측 부하에는 보유 시간/획득 순서를 측정하지 않았으므로 100건의 원인을 starvation이나 디스크 지연 하나로 단정할 수 없습니다.

계측용 추가 mutex의 영향 가능성을 줄이기 위해 후속 진단에서는 원래 잠금을 감싸되 별도 경합 mutex 없이 thread-safe deque append로 집계하고, timeout 시 현재 보유 경과/대기 중 다른 획득 수를 함께 남깁니다. 여전히 관찰 영향이 있을 수 있어 완전 비계측 부하도 유지합니다. 비계측 부하의 실패를 삭제하거나 timeout을 늘리지 않습니다. [동일 코드 PR attempt 2](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41052835100/attempts/2)는 세 번째 반복입니다. Windows 3.12는 다시 비계측 부하의 admission timeout으로 실패했고, 3.13은 admission timeout 1건을 남긴 뒤 CI 시간 제한으로 취소됐습니다. 취소를 통과로 세지 않습니다. 새 진단은 외부 CI 제한으로 요약이 중단돼도 수치를 보존하도록 timeout 숫자 표본을 최대 8개 즉시 출력합니다. CI/요청의 시간 제한과 테스트 강도는 변경하지 않습니다. Windows 3.12/3.13의 후속 결과를 구분해 기록합니다.

### 추가 경합 mutex 없는 진단 · 1e740d1

[코드 1e740d1](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1e740d149acd9e12f29a5bb5c06d1d71d140a894)의 [push 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41055394547/attempts/1)과 [PR 실행](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41055401355/attempts/1)을 추가 비교했습니다. 원래 400/30 함수·native Lock·요청/CI 시간 제한을 유지했습니다.

| 환경 / 실행 | 계측 admission / SQLite | 비계측 admission / SQLite | 전체 결과 |
|---|---|---|---|
| 3.12 push | 0 / 0 | 30 / 0 | 1 failed, 88 passed, 413.70초 |
| 3.12 PR | 6 / 0 | 32 / 0 | 2 failed, 87 passed, 386.82초 |
| 3.13 push | 0 / 0 | 0 / 0 | 89 passed, 360.96초 |
| 3.13 PR | 14 / 0 | 36 / 0 | 2 failed, 87 passed, 455.04초 |

3.12 PR의 계측 timeout 표본은 대기 15000~15016ms, 대기 중 다른 획득 24~29회, 그 순간 현재 보유 경과 78~2406ms입니다. 전체 최대 보유는 2406ms, 획득/해제는 1679/1679, 잔여 대기는 0입니다. 3.13 PR은 timeout 14건, 최대 대기 15014.46ms, 최대 보유 2908.81ms, 획득/해제 5066/5066, 잔여 대기 0입니다. 두 작업의 대기 중 다른 획득 최대는 29, 종료 전 열린 트랜잭션 관측은 0입니다.

**새 재현의 직접 실패 경로는 SQLite OperationalError가 아니라 Python 잠금 획득 15초 초과입니다.** 계측된 실패 구간에서도 다른 쓰기가 진행됐고, 단일 보유가 15초 지속된 정지나 지속적인 슬롯 누수는 관측되지 않았습니다. 누적 큐 대기가 한도를 넘겼다는 사실과, 그 쓰기/스케줄링이 왜 느렸는지는 구분합니다. 이 관측만으로 디스크 지연·GIL/OS 스케줄링·특정 SQLite 호출 중 하나를 근본 원인으로 확정하지 않습니다. 최대 29회라는 수치도 모든 실행에서 FIFO를 증명하지 않습니다.

실패한 작업의 획득 수는 전체 성공 경로의 5200보다 작습니다. 400명의 수집/지급/무결성 검사를 완료했다고 주장하지 않습니다. 원본 검사와 비계측 검사가 실제로 실패했으며 실패를 삭제하지 않았습니다.

로컬 macOS/Python 3.13.5의 같은 진단 코드는 `MAISON_HTTP_TESTS=1`로 **89 passed, 73.44초**였습니다. 숫자 표본만 최대 8개 출력하고 timeout/획득/해제 집계가 맞는지 별도 smoke 검사도 통과했습니다. 로컬 통과가 Windows 성공을 대신하지 않습니다.

## 6. 미검증 사항과 후속 확인

- **확인:** Windows 3.12/3.13의 추가 재현에서 admission timeout을 직접 확인했습니다. 두 실제 실패 분기의 통제 검사는 통과했고, 원본 부하/비계측 부하는 Windows에서 여전히 실패할 수 있습니다.
- **미확정:** 최초 cceed35 요청의 직접 분류 표식은 없습니다. 추가 재현의 지연이 commit, connection close/checkpoint, GIL/OS 스케줄링 또는 다른 비용 중 어디서 발생했는지 계측하지 않았습니다. I/O/호스트 자원 병목이나 starvation을 확정하지 않습니다.
- **다음 조사:** 해당 요청의 commit/close 등 구간 시간과 runner 자원 지연을 제한된 비식별 계측으로 비교해야 합니다. 그 결과에 따라 임계 구간 축소나 대기 정책 변경의 지연/내구성 영향을 검토합니다. 근거 없이 timeout만 늘리거나, 재시도/FIFO 교체/SQLite 내구성 완화로 CI 성공을 만들지 않습니다.
- **병합 상태:** 원본 테스트 실패가 남고 필수 CI가 실패했으므로 PR #3는 초안으로 유지하고 병합하지 않습니다. 한 번 통과한 실행을 해결 근거로 삼지 않습니다. 문서만 추가한 후속 커밋의 CI 상태와 위 코드 검증 결과도 구분합니다.
- **운영 한계:** 실제 Windows 10 카페 노트북의 장시간 운용은 미검증입니다. 현장 서버 배포·고객 DB 수정·기존 v1.4.0 태그/Release 변경은 하지 않았습니다.

Python 3.12/3.13 내부 구현도 같은 것으로 가정하지 않았습니다. [3.12 Windows 잠금 구현](https://github.com/python/cpython/blob/v3.12.10/Python/thread_nt.h)은 조건 변수 기반 경로를, [3.13 acquire](https://github.com/python/cpython/blob/v3.13.16/Modules/_threadmodule.c)와 [PyMutex 구현](https://github.com/python/cpython/blob/v3.13.16/Python/lock.c)은 다른 내부 경로와 공정성 처리를 보여 줍니다. 이 소스 설명만으로 실제 runner의 지연 원인이나 대기 순서를 소급 확정하지 않습니다. 성공/실패가 같은 Python 버전에서도 발생했다는 실행 근거를 우선합니다.

## 7. 관련 근거와 학습 확인

[ADR-001](../decisions/ADR-001-sqlite-persistence.md) · [이전 Windows CI 기록](DEV-003-windows-sqlite-ci.md). 이전 통과 결과는 당시 실행에 대한 사실이며 이번 재발로 신뢰 범위를 보완합니다.

설명할 질문: 같은 503 두 경로를 어떤 증거로 구별하는가? 잠금 순서 미보장이 이번 원인의 증명은 아닌 이유는 무엇인가? 관찰자가 스케줄링에 줄 수 있는 영향과 실제 운영 검증의 차이는 무엇인가?
