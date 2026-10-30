# Maison de Gourmet V1.4 — Windows 운영 패키지

기존 V1.3 Flask + Waitress + SQLite 앱을 그대로 포함한 Windows 10 운영 준비 패키지입니다. 초기 V1.4 패키지의 앱 40개 파일은 원본 V1.3 ZIP과 동일했습니다. GitHub 공개 버전은 Windows CI에서 확인한 DB 파일 잠금 문제를 해결하기 위해 app.py의 초기화 연결 종료만 보완했습니다. UI·API·스키마·QR 키 생성 방식은 변경하지 않았습니다. 기존 실제 DB·운영 설정·인증서·토큰은 이 패키지에 포함하지 않습니다.

**Windows 실기기 실행, 실제 도메인 공개, Cloudflare 연결은 수행하지 않았습니다.** 제공된 Windows 검증표를 통과한 후 카페 운영에 사용하세요. 설치 프로그램도 준비 상태에서는 DB나 서비스를 실행하지 않습니다.

## 시작하기

1. [Windows 설치 설명서](docs/WINDOWS_INSTALL.md)를 읽고, 공식 CPython 3.13 x64 **모든 사용자용 일반 GIL 빌드**를 설치합니다. Python 3.12 x64도 설치 스크립트에서 허용합니다.
2. ZIP 속성에서 출처 차단 여부를 확인하고, 내용을 검토한 뒤 차단 해제하고 압축을 풉니다. `Install.cmd`를 더블클릭하고 관리자 권한을 승인합니다. 기본 경로 `C:\MaisonCafe`에 설치됩니다. 도메인과 이벤트가 미정이면 입력을 비워 준비 설치만 진행합니다.
3. 확정 `config\event.json`과 `config\operations.json`을 준비한 뒤 설치를 다시 실행합니다. 기존 설정과 DB는 덮어쓰지 않습니다. 기존 고객 DB를 옮길 때는 **새 빈 DB 생성 전에** 설명서의 가져오기 절차를 진행합니다.
4. [Cloudflare 안내](docs/CLOUDFLARE.md)에 따라 운영자가 고정 도메인과 터널 토큰을 설정합니다. 이 작업은 외부 공개 단계이므로 준비 설치에 포함되지 않습니다.
5. [Windows 인수 검증표](docs/WINDOWS_ACCEPTANCE.md)를 점검합니다. 직원은 설치된 폴더의 `Manage.cmd`로 상태·재시작·백업을 관리합니다.

## 설치 후 디렉터리

```text
C:\MaisonCafe\
  app\          V1.3 앱 소스 (이 안의 event.json은 배포 예제, 운영 시 미사용)
  config\       실제 event.json, operations.json, 설치/점검 상태, 터널 토큰
  data\         collection.sqlite, 잠금 파일, 별도 복구 검증 DB
  backups\      daily\, weekly\, latest.json, code\, pre-restore\
  logs\         서버/오류/감독 프로세스/백업 기록
  scripts\      설치·시작·중지·재시작·백업·복원·업데이트
  .venv\        전용 Python 환경
  bin\          운영자가 별도 제공한 cloudflared.exe (터널 설정 시 생성)
  Manage.cmd    직원용 관리 메뉴
```

## 제공 파일

| 목적 | 파일 |
|---|---|
| 간편 설치 / 직원 메뉴 | `Install.cmd`, `Manage.cmd` |
| 설치 / 검증 | `scripts/Install.ps1`, `scripts/ops.py` |
| 상태 / 시작 / 중지 / 재시작 / 오류 | `scripts/Control-Server.ps1` |
| 로그인 없는 부팅 실행 / 해제 | `scripts/Register-Autostart.ps1`, `scripts/Unregister-Autostart.ps1` |
| 장애 재시작 감독 | `scripts/Run-Server.ps1` |
| 수동·예약 백업 / 가져오기 / 복구 | `scripts/Backup.ps1`, `scripts/Import-Database.ps1`, `scripts/Restore.ps1` |
| 터널 Windows 서비스 등록 / 해제 | `scripts/Setup-Tunnel.ps1`, `scripts/Remove-Tunnel.ps1` |
| 앱만 안전하게 업데이트 | `scripts/Update.ps1` |
| 기존 설계·실제 명령 분석 | [SOURCE_ANALYSIS.md](docs/SOURCE_ANALYSIS.md) |
| 직원용 짧은 매뉴얼 | [STAFF_GUIDE.md](docs/STAFF_GUIDE.md) |
| 변경사항 / 테스트 결과 | [CHANGELOG.md](CHANGELOG.md), [TEST_REPORT.md](TEST_REPORT.md) |

## 보존·복구 정책

- 앱과 고객 DB·실제 이벤트 설정을 분리합니다. 설치 반복 시 기존 DB를 읽기 전용으로 검증하며 초기화하지 않습니다. 다른 이벤트 ID/상품 ID 조합이면 실행을 거부합니다.
- 서버는 `127.0.0.1`에만 바인딩합니다. 외부 고객 접속은 고정 HTTPS 도메인의 Cloudflare Tunnel을 사용합니다. 공유기 포트 개방은 필요하지 않습니다.
- 서버 작업은 SYSTEM 계정으로 부팅 시 실행됩니다. Waitress 종료 후 감독 프로세스가 10초 후 재시도하고, 감독 프로세스 오류는 작업 스케줄러가 1분 후 재시도합니다. 작업 중복 제한·이름 있는 뮤텍스·DB 운영 파일 잠금으로 중복을 막습니다.
- 매일 03:00(Windows 현지 시간) SQLite 백업 API로 검증된 새 백업을 만듭니다. 일일 14일, 이번 ISO 주 포함 주간 4주를 보존합니다. 외부 백업 실패 시 로컬 백업은 유지하고 실패를 기록합니다. 실패한 회차에는 로컬 오래된 백업도 삭제하지 않습니다.
- 복원은 별도 `data\recovery-check` DB에서 먼저 검증합니다. 실제 활성화는 운영자의 명시적 `RESTORE` 확인, 서버 중지, 현재 DB 안전 백업·원본 분리 후 가능합니다. 자동으로 현재 DB를 덮어쓰지 않습니다.
- 설치 경로와 별도 외부 `MaisonCafe` 백업 폴더는 관리자·SYSTEM 전용 ACL을 적용합니다. 일반 직원도 관리 메뉴 실행 시 관리자 승인이 필요합니다. 관리자 권한이 있는 사용자의 읽기/변경까지 차단하는 방식은 아닙니다.
- 고객 식별 쿠키/복구 링크/10개 수집/지급 완료 1회 정책은 원래 앱을 그대로 사용합니다. 도메인이 바뀌면 쿠키는 자동 이전되지 않습니다. 재발급 복구 링크가 아닌 **기존 DB의 복구 토큰**은 보존되며 링크의 호스트 변경 안내가 필요할 수 있습니다.

## 운영 전 결정할 사항

고정 도메인, 실제 상품명/보상/기간, 실제 이벤트 파일, 기존 고객 DB 이관 여부, 외부 백업 위치, 운영 책임자 및 관리자 계정이 필요합니다. 인증정보는 생성하거나 채워 넣지 않았습니다. 운영 DB는 OneDrive/동기화 폴더·네트워크 공유·USB에 두지 않습니다.

Windows 10 일반 지원은 2025-10-14에 종료되었습니다. 해당 노트북의 보안 업데이트 제공 상태(해당 에디션·연장 지원 자격 포함)를 운영자가 확인해야 합니다. 설치 대상은 Windows 10 22H2 x64(빌드 19045+)이며 이 스크립트가 운영체제 지원을 연장하지는 않습니다. [Microsoft 안내](https://support.microsoft.com/en-us/windows/deployment/updates-lifecycle/windows-10-support-has-ended-on-october-14-2025)

원래 앱의 상세 개발/모바일 검증 안내는 `app/README.md`, V1.3 UI 자료는 `app/` 안 문서를 참고하세요. `app/TEST_REPORT.md`는 과거 버전 보고서이며 이번 결과는 루트 `TEST_REPORT.md`입니다.
