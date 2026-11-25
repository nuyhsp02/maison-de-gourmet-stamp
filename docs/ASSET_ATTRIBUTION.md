# Maison de Gourmet · 공개용 자산 안내

## 스탬프 일러스트 사용 범위

카페가 스탬프 카드에 사용할 그림 10종을 PNG 원본으로 제공했습니다. 2026-11-25 사용자는 이 그림을 **비상업적 포트폴리오 용도로 사용할 허가를 받았다**고 확인했습니다. 공개 목적은 Maison de Gourmet 실제 서비스의 스탬프 화면과 개발 과정을 취업 포트폴리오로 소개하는 것입니다. 허가 범위의 근거는 사용자의 확인입니다.

상업적 이용과 제3자의 자유로운 재사용에 대한 별도 허가는 확인되지 않았습니다. 프로젝트 전체의 오픈소스 라이선스는 미지정입니다. 포함 라이브러리의 고지와 조건은 별도로 관리합니다. [라이브러리 고지](../app/THIRD_PARTY.md)

## 원본 파일과 상품 매핑

상품 ID와 파일명은 앱 카탈로그를 기준으로 작성했습니다. 각 SHA-256은 카페 제공 원본과 저장소 파일의 전체 바이트를 대조한 값입니다. 10개 모두 투명 배경의 117×117픽셀 RGBA PNG입니다. PNG 청크와 파일 끝을 검사해 이미지 데이터와 물리적 픽셀 크기 정보(`pHYs`)만 확인했고, EXIF나 텍스트 청크는 발견되지 않았습니다. [상품 카탈로그](../app/demo-product-catalog.json) · [스탬프 파일](../app/static/stamps/) · [고객 화면 구현 기록](devlog/DEV-011-customer-stamp-ui.md)

| 상품 ID | 스탬프 간략명 | 파일명 | 원본 SHA-256 |
|---|---|---|---|
| `season-01` | 그론도나 틴 기프트박스 | `cookie.png` | `db904e4c9801e386f045fd6b8b58309f7d435f88a3f1f061012748661589d3e4` |
| `season-02` | 피터스야드 크래커 | `cracker.png` | `d425b250f9e4ff8cc7b2853a61c886bfe0c4995700621f52472ae6158e28bda0` |
| `season-03` | 이즈니 처닝버터 | `butter.png` | `85c89f89b85c312a9247114e758c8b89fc8b6475b9c541bb835d29d080f7e6c6` |
| `season-04` | 모나미 생 폴랑 바질 | `sliced-cheese.png` | `88f0799081ac8d9c6690e341780b053f03e6cfefd9288cc8625844c715d1459f` |
| `select-01` | 알랭밀리아 블랙커런트잼 | `jam.png` | `141504c8aa3c4a9f30dd7dcc1d7aa707cb5f257add0185fbb60c494028de027c` |
| `select-02` | 파머스립 쉬라즈 | `wine.png` | `6a33f00eaa6cac40db0d340dbec47f9a9a2d4da76c0861de807857f32762baff` |
| `select-03` | 베리 크리스마스 | `berry-christmas.png` | `6f79bcdde6a77ade940c77a8ae4a2862af6af61d59bd1d8df1f2d5133bd56684` |
| `select-04` | 매우 반짝이는 코 | `reindeer.png` | `b3d3f618d92e026ba6e0e2c0661d996271af7466373d02e246f8728c0307255e` |
| `select-05` | 뱅쇼 | `mulled-wine.png` | `79c9f149006e66048b13febeed08158f805e2730d0c795ebff8a36b821dc2b60` |
| `select-06` | 아이올리브유 | `olive-oil.png` | `8fc96d1137c61cb9d22aba5bd3f4f2561c3592b27171cafd931a63ae23a14ac8` |

## 직원 화면 검증 이미지

직원 화면은 PR 2에서 임시 DB에 합성 카드를 넣어 만든 검증 캡처입니다. 화면의 카드 ID는 합성 카드에 속하며 전화번호 검색란은 비어 있습니다. PNG에는 이미지 헤더·픽셀 데이터·종료 청크만 있습니다. [직원 화면](evidence/admin-pr2-demo.png) · [PR 2 검증 기록](PR2_STAFF_ADMIN.md#실제-검증-결과)
