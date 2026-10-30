# ADR-001 · SQLite 선택 및 데이터 영속성

- 상태: 기존 구현 확인 / 사후 기록. 최초 결정일: **미확인**.
- 기준: [V1.4.0](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f). 적용 요구는 [README](../../README.md), 운영 경로는 [소스 분석](../SOURCE_ANALYSIS.md)에 있습니다.

## 1. 해결하려던 문제

노트북 한 대에서 회원가입 없는 고객의 상품 수집·복구·보상 기록을 보존해야 합니다. 코드 업데이트/서버 재시작 때 기존 고객과 QR 키를 다시 만들면 안 됩니다. 동시 요청에도 상품/지급 중복을 막아야 합니다. 30명/400명은 설계 목표이지 운영 실적이 아닙니다.

## 2. 고려한 대안과 기록의 한계

당시 저장소 후보를 비교한 기록은 없습니다. 회고 시 비교할 수 있는 방안은 서버형 관계형 DB, 브라우저 저장소만 사용하는 구조, 현재 SQLite 파일 DB입니다. 앞의 두 방안을 실제로 설치·측정하거나 비용 때문에 기각했다고 주장하지 않습니다.

## 3. 최종 선택

SQLite의 WAL, 외래키, 트랜잭션, 고유 제약을 사용합니다. 운영 코드는 app, 고객 DB는 별도 data 경로에 둡니다. 기존 이벤트/상품 ID 서명을 확인하고 동일 DB의 QR 키를 유지합니다. 백업은 별도 [ADR-005](ADR-005-windows-and-backup.md) 정책으로 연결합니다.

## 4. 기술적 선택 근거

단일 노트북/기존 스택 유지 제약과 파일 기반 영속성은 현재 구조에 부합합니다. 중복 방지를 브라우저 상태에만 맡기지 않고 DB 기본키와 트랜잭션으로 표현할 수 있습니다. 이는 요구사항과 코드의 연결에 대한 사후 설명이며, 당시 비교 평가표를 복원한 것은 아닙니다.

## 5. 단점과 위험

쓰기 경합이 실제 Windows CI에서 드러났습니다. 현재 단일 프로세스의 쓰기 요청 조정은 다중 서버 간 잠금이 아니며 DB 제약을 대체하지 않습니다. 디스크 고장/분실, 잘못된 복원, 같은 디스크에만 있는 백업도 위험입니다. 파일 DB라는 이유만으로 한 달 안정 운영/실장비 처리량이 검증된 것은 아닙니다.

## 6. 실제 코드 및 테스트 근거

- [schema.sql](../../app/schema.sql): WAL, stamps 복합 기본키, reward_audit 기본키.
- [app.py](../../app/app.py): `init_db`, `connect_db`, `app.write_lock`, 요청 종료 시 연결/잠금 정리.
- [ops.py](../../scripts/ops.py): `validate`, `inspect_database`의 기존 DB 읽기 전용 확인과 이벤트 서명 검사.
- [test_collection.py](../../app/tests/test_collection.py): `test_parallel_duplicate_collect_and_redeem`.
- [test_windows_ops.py](../../tests/test_windows_ops.py): `test_config_space_unicode_existing_db_validation_is_readonly`, `test_other_event_refused_without_overwrite`, `test_real_server_restart_duplicate_process_and_persisted_cookie`.
- [Windows CI 회고](../devlog/DEV-003-windows-sqlite-ci.md): 실패·수정·통과 근거. 합성 부하 테스트는 실사용 고객 수와 구분합니다.

## 7. 향후 개선 가능성

실제 노트북의 대기/실패율·백업 복구 시간을 먼저 측정합니다. 운영 범위가 단일 프로세스를 벗어나면 다른 저장소와 동시성 정책을 새 ADR에서 비교할 수 있습니다. 해당 전환이나 측정은 아직 수행한 것으로 기록하지 않습니다.

### 후속 관측: 병합 후 간헐 503 재발

[DEV-006](../devlog/DEV-006-windows-concurrency-investigation.md)에서 같은 코드의 Windows CI 재발을 조사합니다. 이전의 통과만으로 현재 쓰기 조정이 모든 간헐 실패를 해결했다고 판단하지 않습니다. 원래 사건의 admission timeout/SQLite 오류 분기는 직접 증거가 부족해 미확정이며, 저장소 선택·스키마·잠금/내구성 정책은 변경하지 않습니다.
