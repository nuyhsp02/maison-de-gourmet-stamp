# DEV-002 · 모바일 HTTPS와 QR 스캔 검증 준비

- 기록 유형: 기존 구현·자동 검증의 근거 기반 회고.
- 최초 준비/구현일: **미확인**. 기존 검수 문서의 날짜는 각 문서를 따릅니다.
- 기준: [V1.4.0 / 1284c52](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f).
- 실행 주체: 준비 도구/자동 테스트·CI 기록 확인. 소유자의 실제 휴대폰별 재현 기록은 미확인.

## 1. 문제와 예상 동작

기본 카메라로 QR을 열고 같은 브라우저에서 기록을 이어가야 합니다. 웹 내 연속 스캔은 중앙에 의도한 QR만 판독해야 합니다. 휴대폰에서 노트북의 localhost를 여는 것으로는 노트북 서버에 접속할 수 없으며, 운영 HTTPS·쿠키·카메라 검증을 별도로 준비해야 합니다.

## 2. 관측과 원인

[기존 모바일 안내](../../app/MOBILE_TEST_GUIDE.md)는 신뢰 가능한 HTTPS와 브라우저 저장 공간 차이를 구분합니다. [collection-core.js](../../app/static/collection-core.js)는 표시 영역과 원본 영상의 비율/좌표가 다를 수 있어 crop을 계산합니다. 전체 영상 판독/첫 QR 선택이 잘못된 상품 적립으로 이어질 위험을 다룹니다. 특정 휴대폰에서 실제 오적립을 재현했다는 근거는 저장소에 없습니다.

## 3. 해결 또는 준비 과정

`prepare_mobile_test.py`는 원본을 보존하며 새 폴더에 임시 이벤트·Caddyfile·RUN 안내만 만듭니다. 서버 실행, 인증서 신뢰 설치, DB 생성까지 자동 수행한 것으로 해석하지 않습니다. 운영 공개 경로는 별도의 [Cloudflare 안내](../CLOUDFLARE.md)입니다.

판독은 화면 중앙 영역을 영상 좌표로 바꿔 같은 canvas를 BarcodeDetector/jsQR에 전달합니다. 복수 QR·경계·중심 조건을 확인하고 jsQR은 첫 QR을 가린 2차 판독도 수행합니다. 연속 프레임/처리 중 잠금은 중복 요청을 줄이며 서버 DB 제약은 별도로 유지됩니다.

## 4. 코드 변경

확인할 기존 구현은 [prepare_mobile_test.py](../../app/tools/prepare_mobile_test.py), [collection-core.js](../../app/static/collection-core.js), [app.js](../../app/static/app.js)입니다. Git 도입 이전 구현의 개별 수정 커밋은 미확인입니다. [최초 반입 커밋 1c23cfa](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1c23cfa023785632b801a0055d116bcfb55b6807)은 코드를 들여온 시점이지 최초 구현일이 아닙니다. 이번 문서화에는 코드 변경이 없습니다.

## 5. 검증 결과

- [test_v12.py](../../app/tests/test_v12.py): 원본 보존·DB 미생성·기존 폴더 덮어쓰기 거부·격리 설정의 앱 응답.
- [scanner.test.cjs](../../app/tests/scanner.test.cjs): cover/contain 좌표, 회전된 픽셀, 중앙 밖/복수 QR, 공통 crop.
- [app-flow.test.cjs](../../app/tests/app-flow.test.cjs): 실제 스캐너 함수 흐름을 모형 환경에서 검사, 스캐너 실행/중단 흐름과 중복 요청.
- [V1.4.0 CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41029738350): 전체 Python 84개 및 프런트 41개 통과. 이 총수는 전부 모바일 카메라 검사라는 뜻이 아닙니다.

## 6. 미검증 사항과 후속 확인

아이폰/갤럭시의 실제 카메라, 초점/반사, 회전, 권한 거부, 기본/인앱 브라우저 쿠키, 실제 HTTPS 신뢰·연결 결과는 [인수표](../WINDOWS_ACCEPTANCE.md)에 별도 기록해야 합니다. 테스트 준비 파일 생성은 실물 테스트 완료가 아닙니다.

## 7. 관련 근거와 학습 확인

[ADR-002](../decisions/ADR-002-anonymous-identity.md) · [ADR-003](../decisions/ADR-003-fixed-qr-and-deduplication.md) · [ADR-004](../decisions/ADR-004-flask-waitress-tunnel.md).

설명해 볼 질문: 보이는 사각형을 영상 픽셀로 환산하는 이유는 무엇인가? 같은 crop을 쓰더라도 실기기 검증이 남는 이유는 무엇인가? 소유자의 직접 기기 테스트/설명 여부는 근거를 얻은 뒤 추가합니다.
