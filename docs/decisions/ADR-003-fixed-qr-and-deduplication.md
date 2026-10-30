# ADR-003 · 상품별 고정 QR과 중복 적립 방지

- 상태: 기존 구현 확인 / 사후 기록. 최초 결정일: **미확인**.
- 기준: [V1.4.0](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f), [기존 앱 안내](../../app/README.md).

## 1. 해결하려던 문제

10개 상품을 순서 없이 한 번씩 모아야 하며 같은 상품을 반복 스캔하거나 요청을 재전송해도 중복 적립하면 안 됩니다. 기본 카메라 진입과 웹 안의 연속 스캔을 함께 지원해야 합니다.

## 2. 고려한 대안과 기록의 한계

상품별 고정 QR 사용은 현재 요구/구현에서 확인됩니다. 구매마다 바뀌는 QR, 직원 승인 방식, 고객이 상품 목록을 직접 고르는 방식과의 당시 비교 기록은 없습니다. 회고에서 비교할 수 있는 후보로만 구분합니다.

## 3. 최종 선택

새 DB 초기화 때 상품별 QR 키를 생성해 보존하고 같은 DB에서 동일 URL을 내보냅니다. 공개 상품 ID는 비밀 QR 키와 다릅니다. QR GET은 읽기 전용, 적립은 별도 POST입니다. stamps의 `(customer_id, product_id)` 기본키와 `INSERT OR IGNORE`가 상품별 1회를 보장합니다.

## 4. 기술적 선택 근거

키를 DB에 보관하면 코드/디자인 교체 때문에 QR을 새로 발급할 필요가 없습니다. DB 제약은 클라이언트 중복 감지와 별개로 작동합니다. 프런트는 같은 crop의 단일 중앙 QR·연속 프레임·처리 중 상태를 확인해 오인식/중복 요청을 줄입니다. 상세 좌표 과정은 [모바일 로그](../devlog/DEV-002-mobile-https-and-qr-testing.md)에 연결합니다.

## 5. 단점과 위험

고정 QR을 아는 것은 구매 증명이 아닙니다. 유효 세션에서 정상 탐색으로 URL을 여는 것과 실제 카메라 스캔을 완벽하게 구분하지 못합니다. 자동 적립 조건이 부족하면 수동 확인으로 돌아갑니다. DB/QR 키를 잃거나 도메인을 바꾸면 기존 QR 사용에 영향이 있습니다. 화면 판독의 실기기 정확도는 별도 검증이 필요합니다.

## 6. 실제 코드 및 테스트 근거

- [app.py](../../app/app.py): `init_db`, QR 페이지의 탐색 신호, `collect`와 트랜잭션.
- [manage.py](../../app/manage.py): `qr-export`가 기존 DB의 키를 사용.
- [schema.sql](../../app/schema.sql): products.qr_key UNIQUE, stamps 복합 기본키.
- [collection-core.js](../../app/static/collection-core.js): `sourceRect`, `selectCandidate`, `decodeCrop`, `ScanGate`, `canAutoCollect`.
- [test_collection.py](../../app/tests/test_collection.py): `test_get_head_prefetch_never_create_customer_or_stamp`, `test_parallel_duplicate_collect_and_redeem`.
- [test_ops.py](../../app/tests/test_ops.py): `test_export_fixed_qr_and_live_backup`; [scanner.test.cjs](../../app/tests/scanner.test.cjs): 중앙 밖/복수 QR/회전 픽셀 사례.

## 7. 향후 개선 가능성

실제 매장 조명·반사·카메라·인앱 브라우저를 검증합니다. 구매 증명까지 필요해지면 고정 QR 정책 자체를 다시 결정해야 합니다. 동적 QR/직원 승인 도입은 현재 완료 기능이 아닙니다. 보드의 물리적 크기·덮개·배치는 별도 협의입니다.
