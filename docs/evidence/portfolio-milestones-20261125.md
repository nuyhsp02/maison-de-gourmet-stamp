# Maison de Gourmet · 단계별 검증 근거

기록일 2026-11-25. 코드, 당시 실행 보고, 사용자 확인의 출처를 구분한 공개용 요약입니다. 비공개 보고서는 산출물 묶음 안의 파일명으로 식별합니다. 아래 테스트 수치는 모두 **이전 실행 결과 인용**입니다.

| 단계 | 요구사항과 결과 | 기록 |
|---|---|---|
| V1.4.0 Windows | 노트북 한 대에서 SQLite 기록을 유지하고 재부팅·백업을 준비 | [영속성 결정](../decisions/ADR-001-sqlite-persistence.md) · [Windows와 백업](../decisions/ADR-005-windows-and-backup.md) |
| 공개 저장소·CI | 공개 가능한 소스만 반입. Windows CI 실패·수정과 릴리스 추적 | [Windows CI 회고](../devlog/DEV-003-windows-sqlite-ci.md) · [GitHub와 CI 도입](../devlog/DEV-005-public-github-and-ci.md) |
| 11/20 Ubuntu DEMO | 실제 비공개 HTTPS에서 상품 확인·적립과 기존 데이터 보존 검증 | [DEV-010](../devlog/DEV-010-linux-demo-deployment.md) |
| 11/25 고객 화면 | 등록 이름 표시, 상품 ID에 연결한 그림의 적립·취소 상태 복귀 | [DEV-011](../devlog/DEV-011-customer-stamp-ui.md) |
| 11/25 개인정보·직원 기능 | 합성 등록 시 연락처 암호화, 직원 마스킹, 감사 후 운영자 조회 | [ADR-007](../decisions/ADR-007-contact-privacy.md) · [DEV-012](../devlog/DEV-012-contact-and-staff.md) |
| 11/25 PROD 준비 | 행사 경계·가입 차단·사진·운영 준비도 대조. 이후 사용자 정책 기록 | [ADR-008](../decisions/ADR-008-event-lifecycle.md) · [DEV-013](../devlog/DEV-013-prod-readiness.md) |

저장소의 origin과 공개 GitHub 저장소가 일치합니다. 이력 정비 전 11/25 조회한 공개 main은 `cceed35`이고, 로컬 이력에서 이 커밋은 `feature/contact-privacy-admin`의 `aaccb50`에 포함됩니다. 두 커밋 사이의 로컬 이력은 30개 커밋입니다. GitHub 브랜치 검색과 최신 로컬 커밋 조회에서는 해당 브랜치와 커밋이 확인되지 않았습니다. 아래 식별자는 당시 배포·검증 보고서의 원래 커밋입니다. 이메일 정리 단계의 파일 트리는 당시와 같았고, 최종 후보에서는 PR 설명 문서의 내부 링크만 바뀌었습니다. 배포 당시 식별자와 두 차례의 정비 결과는 [DEV-015 대응표](../devlog/DEV-015-public-history-and-asset-permission.md)에 있습니다. [확인한 공개 main 커밋](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/cceed35e3cc9188ab646ea7b3861629b2c977064)

## 시연 배포

**원자료** `DEMO-DEPLOYMENT-ce0b22b.md`의 ‘백업과 전환’ 및 ‘배포 후 외부 HTTPS에서 확인한 결과’. 보고서가 가리키는 Ubuntu 전체 회귀와 외부 스모크 로그도 대조했습니다.

11/20 기존 Linux 비공개 DEMO의 `b4ecfc3`을 `ce0b22b`로 전환했습니다. 전환 전에 별도 장치에서 암호화 백업을 복원하고 기존 행·상품 키를 대조했습니다. 이전 Ubuntu 회귀는 Python 217건·JavaScript 68건 통과, 배포 후 검사는 외부 HTTPS 11개 항목 통과입니다. 이때 추가한 카드는 합성 데이터입니다. **매장 현장 검수 완료는 사용자 확인**이며, 정확한 현장 검수 날짜·기기별 원기록은 미확인입니다.

## 스탬프 화면

**원자료** `stamp-ui-20261125/REPORT.md`, 같은 묶음의 `linux-results.txt`와 적립 전·후·직원 취소 후 비교. `cf6b88f` Ubuntu 회귀는 Python 219건·JavaScript 70건 통과입니다. Mac의 모바일 Chrome 에뮬레이션에서 7개 화면 너비와 80자 합성 이름을 확인했습니다. 10개 원본 PNG가 배포 후보 파일과 바이트 단위로 일치했고, 한 상품의 적립 전과 취소 후 그림 영역이 다시 일치했습니다.

## 연락처 보호

**원자료** `contact-privacy-20261125/REPORT.md`, `contact-privacy-20261125/linux-results.txt`, 같은 묶음의 `mac-results.txt`와 `browser-results.txt`. `aaccb50` Ubuntu 전체 로그는 Python 227건 통과, JavaScript 73건 통과, 종료 코드 모두 0입니다. Mac 루프백 SSH 검증에서 감사 저장과 전달·표시 단계를 구분했고, 별도 암호로 보관한 키와 백업을 복원해 합성 연락처 한 건을 열었습니다. 실고객 처리나 운영망 검수 실적은 없습니다.

## 출시 준비도

**원자료** `PROD-READINESS-20261125.md`의 개인정보 대조표, 기간 검산표, PROD 인프라·롤백 점검. `aaccb50` 소스의 날짜 판정 함수만 메모리에서 여섯 시점으로 대조했습니다. 2/1 적립 차단·2월 보상 허용·3/1 보상 차단을 확인했습니다. 서버 접속이나 전체 회귀 재실행은 없었습니다. 조사 당시에는 직원 취소에 날짜 제한이 없었고 3월 고객 접근 종료 조건도 없었습니다.

## 사용자 결정

| 출처 | 확인한 내용 |
|---|---|
| 결정 원장 D80~D82 | 매장의 표시·인쇄·접근·직원 요구, 전체 연락처 보존 요청, 이름·일러스트 로컬 검증 |
| D83~D85 | 전화 입력 요구, HMAC 유지와 암호화·마스킹·운영자 분리 설계, 합성 구현 결과. 당시 보관 조건은 검토 중 |
| D86~D87 | 적립과 보상 마감 분리. 당시 2월 취소와 행사 후 조회 종료는 후속 검토 사항 |
| 11/25 사용자 후속 지시 | 미수령 상품 연락을 위한 암호화 보관, 단일 안내 확인 체크박스, 판촉 제외. 2/1 일반 직원 취소 차단·관리자 예외 정정 시 사유와 감사 필수. 3/31 00:00 KST 고객 접근 종료·개인정보 파기 기준 확정 |

마지막 행은 1차 준비도 보고 이후에 받은 결정입니다. 당시 보고서의 미결 항목을 그대로 인용할 때는 이 순서를 함께 표기합니다. 법적 처리 근거와 백업 보유기간은 계속 미확정입니다. 신규 PROD 운영 지표와 공개 이력은 아직 없습니다.
