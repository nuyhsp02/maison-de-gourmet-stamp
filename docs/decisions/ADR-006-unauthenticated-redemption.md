# ADR-006 · 직원 인증 없는 보상 지급 처리와 위험 수용

- 상태: 요구 정책/기존 구현 확인 / 사후 기록. 최초 결정일: **미확인**.
- 기준: [V1.4.0](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f), [직원 안내](../STAFF_GUIDE.md), [배포 전 위험](../../app/PRE_DEPLOY_CHECKLIST.md).

## 1. 해결하려던 문제

10종을 모은 고객에게 직원이 실물 보상을 지급한 뒤, 고객 화면에서 추가 인증/체크박스/확인창 없이 버튼 한 번으로 완료를 기록해야 합니다. 반복 요청·응답 유실에도 지급 기록은 한 번이어야 합니다.

## 2. 고려한 대안과 기록의 한계

직원 PIN/로그인 없이 처리하는 정책과 위험 경고는 문서에 있습니다. 직원 인증, 별도 승인 단계, 일회용 지급 코드를 실제 비교/실험한 기록은 없습니다. 이들은 사후 재검토 후보이며 현재 구현/당시 검증 결과로 쓰지 않습니다.

## 3. 최종 선택

고객 세션의 `redeem` POST가 10종 수집과 지급 기간을 검사하고 redeemed_at 및 고객별 감사 기록을 트랜잭션으로 남깁니다. 이미 완료된 요청에는 완료 상태를 반환합니다. UI는 완료 버튼을 다시 활성화하지 않고 인증 부재 위험을 안내합니다.

## 4. 기술적 선택 근거

한 번의 조작이라는 명시적 운영 요구를 유지하고 중복은 서버/DB에서 막습니다. `confirm_received` 값은 고객 화면의 확인 의사이며 **직원 신원이나 실물 지급의 증명은 아닙니다**. 이 선택은 개발 요구에 따른 위험 수용 기록이지 실제 매장의 승인 서명·직원 교육 완료 기록은 아닙니다.

## 5. 단점과 위험

고객도 임의로 버튼을 누를 수 있고, 실물 미지급·오조작을 서버가 구분하지 못합니다. 반복 요청 방지는 실제 중복 실물 지급을 자동 예방하는 시스템과 다릅니다. 오래된 DB를 복원하면 이후 실제 지급 이력이 빠질 수 있습니다. 원래 정책에는 사용자 초기화/취소 절차가 없습니다.

## 6. 실제 코드 및 테스트 근거

- [app.py](../../app/app.py): `redeem`, 10종/기간/기지급 검사, 트랜잭션.
- [schema.sql](../../app/schema.sql): reward_audit.customer_id 기본키, method의 `unauthenticated_customer_screen` 제약.
- [app.js](../../app/static/app.js), [index.html](../../app/templates/index.html): 지급 버튼 상태·경고·1회 조작.
- [test_collection.py](../../app/tests/test_collection.py): `test_completion_redemption_confirmation_and_permanent_collection`, `test_parallel_duplicate_collect_and_redeem`, `test_reward_after_deadline_rejected`.
- [app-flow.test.cjs](../../app/tests/app-flow.test.cjs): 'one reward button directly posts, no checkbox/dialog; state survives reread', 'lost reward response is reconciled to completed state without an active retry button'.

## 7. 향후 개선 가능성

실물 지급 절차·오조작 대응·복원 후 지급 내역 대조를 운영 전에 확인합니다. 인증 부재 위험이 더 이상 수용되지 않으면 승인 방식과 UI 조작 횟수를 새 ADR로 재결정해야 합니다. 이번 문서는 PIN/로그인이나 새로운 지급 취소 기능을 추가하지 않습니다.
