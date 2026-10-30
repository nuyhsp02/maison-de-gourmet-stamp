# DEV-004 · Windows 운영 패키지와 자동 백업 설계

- 기록 유형: 기존 패키지 구현/자동 검사 근거 기반 회고.
- 최초 구현 시작일: **미확인**. 기존 [검수 보고서](../../TEST_REPORT.md)의 V1.4 검수일은 2026-10-30입니다.
- 코드 확인 기준: [최초 Git 반입 1c23cfa](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1c23cfa023785632b801a0055d116bcfb55b6807), [V1.4.0 / 1284c52](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f).
- 실행 주체: AI 지원 구현/자동 검사와 CI 기록. 소유자의 실제 카페 노트북 설치/운영 경험은 기록만으로 확인하지 않음.

## 1. 문제와 예상 동작

직원이 전원을 켜는 운영을 준비하되 코드 업데이트가 고객 기록을 지워서는 안 됩니다. 실행 중 DB의 안전한 사본, 기간별 보존, 외부 경로 실패와 복원 실수 대응이 필요합니다.

## 2. 관측과 설계 근거

[소스 분석](../SOURCE_ANALYSIS.md)의 기본 경로는 앱 안의 이벤트/DB입니다. 운영 경로를 별도로 명시하지 않으면 코드와 데이터를 섞어 관리할 위험이 있습니다. 이것은 확인한 구조상의 위험이지 실제 고객 데이터를 잃은 사고 기록은 아닙니다.

운영 중 WAL DB의 단순 파일 복사를 피하고 SQLite 백업 API를 사용하도록 기존 구현/안내가 구성돼 있습니다. 디스크 고장·USB 분리·예약 작업 실패를 실제 카페에서 겪었다고 주장하지 않습니다.

## 3. 구현 과정에서 확인되는 선택

앱/설정/DB/백업/로그/가상환경을 분리하고 실제 설정이 미확정이면 준비 설치만 수행합니다. SYSTEM 예약 작업, 감독 재시도, 중복 실행 제한, 상태 메뉴를 제공합니다. 이 구현의 최초 단계별 작업 시각은 Git 반입 전이므로 복원하지 않습니다.

백업은 새 임시 파일에 API 사본 → 무결성/외래키/이벤트 확인 → 게시 순서입니다. 일일 14일/주간 4주를 정리하며 외부 실패 시 로컬 백업을 보존하고 실패를 알립니다. 복원은 별도 후보 검증, 명시적 활성화, 현재 DB 백업/원본 분리로 구성합니다.

## 4. 코드 변경

[Install.ps1](../../scripts/Install.ps1), [Register-Autostart.ps1](../../scripts/Register-Autostart.ps1), [Run-Server.ps1](../../scripts/Run-Server.ps1), [Backup.ps1](../../scripts/Backup.ps1), [Restore.ps1](../../scripts/Restore.ps1), [ops.py](../../scripts/ops.py)의 기존 구현이 근거입니다. 최초 Git 반입을 각 기능의 최초 개발 커밋으로 부르지 않습니다. 이번 작업에는 설치/백업 코드 변경이 없습니다.

## 5. 검증 결과

[test_windows_ops.py](../../tests/test_windows_ops.py)에서 실제 임시 SQLite 사본/복원, 기존 목적지 거부, 원본 보존, 공간 부족, 외부 실패, 보존 날짜 경계를 검사합니다. [V1.4.0 CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41029738350)의 84개 통과에는 이 검사들이 포함됩니다.

보존 기간 검사는 지정한 날짜의 테스트 파일로 수행한 것입니다. 실제 14일/4주 예약 백업 운용 실적이 아닙니다. [http-repeat.txt](../evidence/http-repeat.txt)의 5회 반복은 임시 loopback 서버 검사 2개이며 Windows 재부팅·SCM 서비스 복구 5회 검사가 아닙니다. PowerShell 파서/보조 함수 통과도 설치 스크립트 전체 실행과 다릅니다.

## 6. 미검증 사항과 후속 확인

로그인 전 시작, SYSTEM/ACL, 절전·배터리·전원 중단, 실제 외부 매체/UNC 공유, 터널 서비스, 현장 업데이트/롤백은 [Windows 인수표](../WINDOWS_ACCEPTANCE.md)에서 별도 확인해야 합니다. 데이터 손실 허용 범위·복구 시간 목표/실측도 미확인입니다.

## 7. 관련 근거와 학습 확인

[ADR-005](../decisions/ADR-005-windows-and-backup.md) · [백업/복원 안내](../BACKUP_RECOVERY.md) · [설치 설명서](../WINDOWS_INSTALL.md).

설명해 볼 질문: 안전한 사본을 만든 것과 복원 가능성을 검증한 것은 어떻게 다른가? 같은 디스크에만 백업할 때 남는 위험은 무엇인가? 소유자의 실제 매체/장비 검증은 증거와 함께 추후 추가합니다.
