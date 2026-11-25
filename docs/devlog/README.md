# 개발 로그

오류를 실제로 관측했는지, 원인을 확인했는지, 어떤 변경과 검증이 남아 있는지 연결합니다. 회고를 새로 작성했다고 과거의 직접 경험이 생기는 것은 아닙니다. [기록 원칙](../PORTFOLIO_RECORDS.md)을 함께 적용합니다.

| 기록 | 다루는 내용 | 현재 확인 수준 |
|---|---|---|
| [DEV-001 · 캐시/JavaScript 버전 불일치](DEV-001-browser-cache-and-js-version.md) | 현재 캐시 정책과 누락된 재현 근거 | 실제 발생일·원인·해결 커밋 **미확인** |
| [DEV-002 · 모바일 HTTPS·QR 검증 준비](DEV-002-mobile-https-and-qr-testing.md) | 격리 설정·crop 판독·자동 검증 범위 | 코드/자동 검사 확인, 실물 단말 결과 없음 |
| [DEV-003 · Windows SQLite CI](DEV-003-windows-sqlite-ci.md) | 연결 잔류·쓰기 경합·수정 후 통과 | 실패 실행·커밋·최종 CI 확인 |
| [DEV-004 · Windows 패키지·백업](DEV-004-windows-package-and-backups.md) | 데이터 분리·예약·안전 백업/복원 | 구현/자동 검사 확인, 현장 운영 미확인 |
| [DEV-005 · Public GitHub·CI](DEV-005-public-github-and-ci.md) | 최초 Git 이력·공개 검수·PR/Release | Git/PR/Release/CI 기록 확인 |

위 DEV-001~005의 공통 구현 기준은 [V1.4.0 / 1284c52](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f)입니다. Git 도입 전 사건의 최초 날짜/수정 커밋은 없을 수 있습니다. 기존 검수 문서에 적힌 날짜와 실제 문제 발생일을 구분합니다.

## 새 기록 작성

[TEMPLATE.md](TEMPLATE.md)를 복사해 다음 빈 번호와 내용을 설명하는 파일명을 정합니다. 템플릿의 미확인 항목을 무리하게 채우지 않습니다. 작성 후 이 목록에 링크를 추가하고 실제 작업 PR에 연결합니다. 별도 앱/CI/PR 템플릿 변경은 필요하지 않습니다.

문제 해결이 아닌 조사·검증 준비도 기록할 수 있습니다. 다만 '재현됨/해결됨'과 '가설/미실행'을 상태에 표시합니다. 자동 테스트 총수를 특정 기능의 실물 테스트 수처럼 쓰지 않습니다. CI 로그가 보존 기간 후 열리지 않을 수 있으므로 핵심 결과와 한계를 문서에 남기되 운영 비밀/원본 민감 로그는 복사하지 않습니다.

- [DEV-006 조사 원본 연결](DEV-006-windows-concurrency-investigation.md)
- [DEV-007 B 구현 후보 및 실제 검증](DEV-007-transaction-write-scope.md) · [백엔드 학습](../BACKEND_LEARNING.md)
- [DEV-008 공식 Python 3.15.2 런타임/설치 호환성](DEV-008-python315-runtime.md)

- [DEV-009 B admission 한정 조사: 원인 미확정·운영 수정 보류](DEV-009-b-admission-bounded-investigation.md)

## 비공개 시연에서 정식 운영 준비까지

| 기록 | 기준 | 주요 근거 |
|---|---|---|
| [DEV-010 · Ubuntu DEMO 배포](DEV-010-linux-demo-deployment.md) | `b4ecfc3 → ce0b22b` | 배포 전 격리 복원과 외부 HTTPS 검증 |
| [DEV-011 · 카드 소유자의 이름과 스탬프 그림](DEV-011-customer-stamp-ui.md) | `ce0b22b → cf6b88f` | 합성 카드 적립·취소 전후 비교 |
| [DEV-012 · 연락처 보관과 직원 인증](DEV-012-contact-and-staff.md) | `cf6b88f → aaccb50` | 원자적 이관, 직원 마스킹 및 감사 후 한 건 조회 |
| [DEV-013 · PROD 출시 준비도 조사](DEV-013-prod-readiness.md) | `aaccb50` 조사 | 날짜 경계 검산, 정책과 코드의 차이, 후속 결정 |
| [DEV-014 · 공개 저장소 이력 및 개인정보 공개 검수](DEV-014-public-history-review.md) | `ed9912a` 이후 8개, 1차 정비 `948f948` | 공개 22개 보존, 작성자·커미터 검사, 내부 링크와 이미지 권한 차단 |
| [DEV-015 · 공개용 Git 이력 정비 및 이미지 사용 권한 확인](DEV-015-public-history-and-asset-permission.md) | 2차 정비 `bbfb27b` | 과거 내부 링크 제거, 후보 이력 검사, 원본 10종의 비상업적 포트폴리오 사용 범위 |
| [DEV-016 · 공개 절차 Fail-Closed 및 보안 검수 강화](DEV-016-publication-fail-closed.md) | `bbfb27b` 위 미커밋 작업 | 승인 SHA·비공개 대조 자료 확인, 개인키 탐지와 패키징 오류 출력 검증 |

각 로그 끝에는 소유자가 합성 환경에서 직접 재현하고 자기 말로 설명한 뒤 채울 칸이 있습니다. [단계별 검증 근거](../evidence/portfolio-milestones-20261125.md)
