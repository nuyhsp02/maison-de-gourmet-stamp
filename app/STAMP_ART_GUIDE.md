# V1.3 · 사진 기반 라인드로잉 교체 안내

현재는 기존의 간단한 음료·베이커리 아이콘을 상품별 임시 SVG 심볼로 분리했습니다. **실제 상품 사진이나 사진 기반 드로잉은 아직 넣지 않았고, 사진 자동 변환 기능도 추가하지 않았습니다.**

## 교체할 파일

| 파일/구조 | 역할 | 이후 수정 |
| --- | --- | --- |
| static/stamp-art.svg | 상품별 SVG symbol 10개 | 사진에서 만든 라인드로잉의 path로 해당 symbol 내부만 교체 |
| static/stamp-art.js | 기존 상품 ID → symbol ID 매핑 | symbol ID를 그대로 쓰면 수정 불필요. 새 그림을 연결할 때만 매핑 확인 |
| static/app.js의 renderStampArt() | 고정 그림 영역과 SVG를 생성 | 일반적인 그림 교체에서는 수정 불필요 |
| static/style.css의 .stamp-art | 방울 안 그림 크기·중앙 정렬·잘라내기 | 일반적인 그림 교체에서는 수정 불필요 |

상품 ID, event.json, QR URL, 고객 데이터, 스캔/지급 API는 그림 교체를 위해 변경하지 않습니다. 등록되지 않은 상품 ID는 기존 아이콘으로 표시됩니다.

## 기존 상품과 심볼

| 상품 ID | SVG symbol ID |
| --- | --- |
| season-01 | menu-season-01 |
| season-02 | menu-season-02 |
| season-03 | menu-season-03 |
| season-04 | menu-season-04 |
| select-01 | menu-select-01 |
| select-02 | menu-select-02 |
| select-03 | menu-select-03 |
| select-04 | menu-select-04 |
| select-05 | menu-select-05 |
| select-06 | menu-select-06 |

SVG 파일은 symbol을 담은 묶음이므로 단독으로 열면 빈 화면일 수 있습니다. 확인은 서버로 연 스탬프판에서 합니다.

상품명은 기존 event.json에서 계속 읽습니다. 그림 파일명이나 심볼을 바꿔도 상품 자체의 식별자를 바꾸지 마세요.

## 드로잉 규격

- viewBox는 **0 0 48 48**로 유지합니다.
- 핵심 실루엣은 대략 8~40 범위에 두고 선이 프레임 밖으로 나가지 않게 합니다.
- 선 굵기는 2를 시작값으로 사용하고, 휴대폰의 실제 크기에서 복잡한 세부를 줄입니다.
- `stroke="currentColor"`, `fill="none"`을 기본으로 사용합니다. 검정/흰색/레드 등을 path에 고정하면 미수집/완료의 자동 색상 전환이 깨질 수 있습니다.
- 사진 전체를 `<image>`로 넣는 대신 간단한 라인과 면으로 구성한 벡터를 사용합니다. 스크립트·외부 이미지·폰트·이벤트 핸들러를 포함하지 않습니다.
- 상품명·번호·완료 체크·도장 이중 테두리는 그림 안에 그리지 않습니다. 앱이 별도로 표시합니다.

형식 예시입니다. 아래 path는 임시 컵 모양이며 실제 상품 사진에서 만든 결과가 아닙니다.

```xml
<symbol id="menu-season-01" viewBox="0 0 48 48">
  <g fill="none" stroke="currentColor" stroke-width="2"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 16h22v12c0 7-4 11-11 11s-11-4-11-11V16Z"/>
    <path d="M34 19h3a5 5 0 0 1 0 10h-3M10 42h27"/>
  </g>
</symbol>
```

## 크기와 상태의 분리

`.stamp-art`는 방울 지름의 52%인 정사각형입니다. 내부 SVG는 xMidYMid meet로 맞추며 프레임 밖은 잘라냅니다. 수집 상태가 바뀌어도 이 영역은 같은 크기를 유지합니다.

미수집은 옅은 아이콘, 완료는 선명한 아이보리 아이콘으로 바뀝니다. 도장의 붉은 채움·이중 링·체크는 그림과 다른 요소이므로 새 드로잉이 상태 표현을 대신하거나 가리지 않습니다. 외부 SVG는 같은 출처의 `/static/stamp-art.svg#심볼ID`로 불러옵니다. [SVG use 설명](https://developer.mozilla.org/en-US/docs/Web/SVG/Reference/Element/use)

## 교체 후 확인

1. symbol ID·viewBox·상품 매핑이 맞는지 확인합니다.
2. JS/HTML/SVG 파일을 함께 보존하고 웹 서버에서 열어 확인합니다. HTML을 file://로 열어 검증하지 않습니다.
3. 미수집/수집 완료 두 상태에서 선 색·밀도·잘림과 이중 링/체크 간섭을 확인합니다.
4. 320px 작은 화면과 일반 iPhone/갤럭시에서 실제 크기로 봅니다. 확대 화면에서만 예뻐 보이는 세부는 줄입니다.
5. 단색 선이 실제 Safari/Chrome에서 올바르게 상속되는지 확인합니다.
6. 기존 테스트를 실행합니다. viewBox/ID/등록 개수/외부 이미지 배제 등의 계약이 바뀌면 테스트가 실패합니다.

사진을 제공할 때는 어느 상품 ID의 사진인지 함께 알려 주세요. 사진을 받아 드로잉을 만든 뒤에도 원본 상품·QR·DB 구조를 바꿀 필요는 없습니다.
