# ADR-002 · 회원가입 없는 익명 고객 식별

- 상태: 기존 구현 확인 / 사후 기록. 최초 결정일: **미확인**.
- 기준: [V1.4.0](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f), [README의 익명 식별과 개인 복구](../../README.md#익명-식별과-개인-복구).

## 1. 해결하려던 문제

고객에게 가입/로그인을 요구하지 않으면서 같은 브라우저의 수집판을 재인식해야 합니다. 쿠키를 잃거나 브라우저가 달라질 때는 고객이 선택적으로 기록을 복구할 수 있어야 합니다.

## 2. 고려한 대안과 기록의 한계

회원가입을 사용하지 않는 조건은 확인됩니다. 계정 로그인, 기기 특성으로 식별, 브라우저 저장소의 식별자만 사용하는 방안과 실제 비교 실험을 한 기록은 없습니다. 이들은 사후 비교 후보이며 당시 기각 과정을 뜻하지 않습니다.

## 3. 최종 선택

새 고객 버튼의 POST로 고객/세션을 생성합니다. 불투명한 세션 토큰은 운영 HTTPS의 Secure·HttpOnly·SameSite=Lax·Path=/ 쿠키에, 토큰 해시는 DB에 저장합니다. 복구 링크는 선택적으로 발급하고 fragment에 토큰을 넣습니다. 복구 POST가 해시/만료를 검사하며 재발급하면 이전 복구 링크를 무효화합니다.

## 4. 기술적 선택 근거

현재 요구는 사람의 신원 인증보다 수집 기록의 재인식입니다. 쿠키/서버 세션과 별도 복구 경로가 이 흐름을 구현합니다. GET으로 새 고객을 만들지 않으며 기존 세션의 변경 요청은 Origin/CSRF 검증을 거칩니다. 원문 대신 해시를 보관하는 구현도 확인됩니다.

## 5. 단점과 위험

브라우저/도메인이 달라지면 같은 휴대폰이라고 자동으로 같은 고객이 되지 않습니다. 쿠키 삭제·차단·만료와 인앱 브라우저 분리가 영향을 줍니다. 개인 복구 링크를 가진 사람이 복구할 수 있어 공유/유출 위험이 있습니다. 여러 익명 기록을 만든 사용자를 실제 사람 한 명으로 제한하는 구조가 아닙니다.

## 6. 실제 코드 및 테스트 근거

- [app.py](../../app/app.py): `identity`, `new_session`, `set_cookie`, `recovery_link`, `recover`.
- [schema.sql](../../app/schema.sql): sessions/recovery의 token_hash, expires_at, 고객 외래키.
- [test_collection.py](../../app/tests/test_collection.py): `test_explicit_start_cookie_session_and_unique_products`, `test_recovery_hashes_rotation_conflict_and_preserves_redeemed`, `test_expired_session_cannot_collect`, `test_production_secure_cookie_headers_and_period_boundaries`.
- [test_v11.py](../../app/tests/test_v11.py): `test_v1_data_cookie_recovery_link_and_fixed_qr_survive_restart`.
- [모바일 검증 안내](../../app/MOBILE_TEST_GUIDE.md)와 [인수표](../WINDOWS_ACCEPTANCE.md): 자동 테스트와 실기기 확인의 경계.

## 7. 향후 개선 가능성

실제 브라우저별 저장/복구 흐름과 고객 안내를 검증합니다. 기록 병합이나 별도 본인 확인을 도입한다면 새 데이터/위험 정책이 필요합니다. 현재는 구현하거나 개인정보 수집을 추가하지 않았습니다.
