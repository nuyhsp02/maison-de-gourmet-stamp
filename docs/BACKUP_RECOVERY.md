# 백업·복원

명령은 관리자 PowerShell에서 실행합니다. 기본 설치 경로가 다르면 해당 경로로 바꿉니다. 운영 중인 DB를 탐색기로 복사하거나 옛 백업으로 무조건 덮어쓰지 않습니다.

## 자동/수동 백업

```powershell
& "C:\MaisonCafe\scripts\Backup.ps1"
```

매일 예약 작업과 같은 명령입니다. SQLite 읽기 전용 원본 연결 → SQLite 백업 API → 새 임시 파일 → integrity_check + foreign_key_check + 이벤트 서명 검사 → 파일 동기화 → 새 파일 게시 순서입니다. 읽기 전용 점검이 WAL 관련 SQLite 보조 파일을 다룰 수는 있지만 고객 레코드/스키마를 초기화하거나 갱신하지 않습니다.

- 일일: `backups\daily\maison-YYYYMMDDTHHmmss-회차.sqlite`. 오늘 포함 최근 **14일**에 속하는 파일을 보존합니다. 같은 날 수동 백업을 여러 번 하면 모두 보존합니다.
- 주간: `backups\weekly\maison-week-ISO연도-ISO주-회차.sqlite`. ISO 월요일 시작, 이번 주 + 이전 3주 보존. 해당 주 첫 성공 백업 1개를 유지합니다.
- 날짜는 Windows 현지 시각입니다. 날짜를 잘못 설정했다가 되돌리면 보존 판정이 달라지므로 먼저 시간을 확인합니다. 서버가 꺼져 놓친 날/주에 대한 기록을 과거 상태로 만들어내지는 않습니다.
- 검증된 새 백업이 만들어진 뒤에만 이름 규칙에 맞는 오래된 백업을 정리합니다. 임의의 수동 파일은 자동 삭제 대상이 아닙니다. 다른 이벤트/다른 설치의 백업 폴더와 섞지 마세요.
- 외부 경로는 `operations.json`의 `ExternalBackupPaths` 배열입니다. 각 경로 아래 전용 `MaisonCafe\daily`, `MaisonCafe\weekly`를 만듭니다. 관리자는 이 전용 폴더에만 관리자/SYSTEM ACL을 적용합니다. 기존 자료 폴더 전체의 권한을 바꾸지 않습니다.
- 외부 경로 접근/ACL/공간 부족 시 로컬 백업은 시도·보존하고 `partial`로 실패 반환합니다. 실패한 회차는 로컬 오래된 백업도 정리하지 않습니다. Windows 예약 작업이 재시도할 수 있습니다. 로컬 공간까지 부족하면 백업을 만들지 못합니다.
- 최소 여유는 `MinimumFreeMB`(기본 256MB)와 원본 크기 3배+16MB 중 큰 값입니다. 검사 후 다른 프로그램이 공간을 소모하는 경우 실패할 수 있으므로 성공 여부를 꼭 확인합니다.

결과는 `backups\latest.json`, 실패 시 `backups\last-failure.json`, `logs\operations-YYYY-MM-DD.jsonl`와 감독 로그에 기록됩니다. 최신 성공보다 마지막 실패 시각이 늦으면 조치가 필요합니다. 상태 메뉴에서 예약 작업 결과와 다음 실행도 확인합니다. 별도 이메일/문자 알림은 없으므로 직원이 매일 확인합니다.

운영 노트북과 같은 디스크의 백업만으로는 디스크 고장에 대비할 수 없습니다. 별도 NTFS USB 등 외부 백업을 설정하고 다른 장치에서 백업을 읽어볼 수 있는지 확인하세요. USB 드라이브 문자가 바뀌면 설정을 수정합니다. 동기화 프로그램은 닫힌 백업 파일만 다루고 **운영 DB/WAL 폴더는 동기화하지 않습니다**.

## 1단계: 현재 DB를 그대로 두고 복구 검증

```powershell
& "C:\MaisonCafe\scripts\Restore.ps1" -BackupFile "E:\CafeBackup\MaisonCafe\daily\실제백업파일.sqlite"
```

`data\recovery-check\restore-회차.sqlite` 별도 후보 DB를 만들고 무결성·외래키·이벤트 서명·고객/스탬프/지급 레코드 수를 검사합니다. 후보 해시와 원본 경로를 `.receipt.json`에 기록합니다. **현재 운영 DB는 교체하지 않습니다.** 기존 DB를 보존한 채 이 단계만 수행해 복구 가능성을 정기 점검할 수 있습니다. 이 단계는 손상 여부/레코드 수 검증이며 모든 고객 화면의 의미까지 대신 확인해 주지는 않습니다.

## 2단계: 담당자 판단 후 후보 활성화

직원용 메뉴에는 현재 DB를 실제로 교체하는 버튼이 없습니다. 담당자가 점검 시간에만 실행합니다.

```powershell
& "C:\MaisonCafe\scripts\Restore.ps1" -Activate -Candidate "C:\MaisonCafe\data\recovery-check\검증된후보.sqlite"
```

1. 백업 시점 이후의 적립/보상 지급은 복구 후보에 없을 수 있습니다. 실물 보상 지급 내역과 대조해 중복 지급을 예방합니다.
2. 정확히 `RESTORE`를 입력해야 진행합니다. 서버를 중지하고 자동 실행을 일시 정지합니다.
3. 후보 해시가 변했거나 서버/백업 잠금이 남아 있으면 거부합니다.
4. 현재 DB를 SQLite API로 `backups\pre-restore`에 안전 백업합니다. 현재 원본 DB와 존재하는 WAL/SHM/journal은 `data\retired\회차`로 함께 이동해 보존합니다.
5. 검증된 후보에서 새 DB를 만든 뒤 활성화합니다. 파일 교체 중 오류 시 원본 경로 복원을 시도하고 자동 시작하지 않습니다. 원본·별도 백업은 삭제하지 않습니다.
6. 별도 복제 DB의 로컬 HTTP 검사를 한 뒤에도 서버는 **점검 모드로 유지**합니다. 담당자가 레코드 수·이벤트·보상 상태를 확인한 후 메뉴 `2 시작`으로 운영을 재개합니다.

후보를 SQLite 편집기로 수정하면 해시가 달라져 활성화되지 않습니다. 원본 백업으로 1단계부터 다시 검증하세요. 여러 차례 실패하면 자동으로 계속 복원하지 말고 원본/보존 폴더/로그를 그대로 두고 담당자에게 전달합니다.

`pre-restore`, `retired`, `recovery-check`, `backups\code`는 자동 보존 기간 삭제 대상이 아닙니다. 정상 운영 재개와 별도 외부 백업을 확인한 담당자가 수동 정리해야 합니다. 검증 후보와 로그도 고객 정보로 취급합니다.

## 로그 공간 관리

`server.log`, `errors.log`는 각각 5MB × 현재 파일+5개로 순환합니다. 서버 표준 출력/오류는 감독 프로세스가 시작 시 5MB 초과 파일을 별도 보관합니다. 감독/운영 로그는 날짜별이며 한 달 운영 후 보관 여부를 결정합니다. 터널은 error 수준 `logs\tunnel.log`이고 자체 순환을 보장하지 않으므로 상태 메뉴의 50MB 경고를 확인합니다.

터널 로그 정리는 서비스 중지 → 로그를 별도 보관 → 서비스 시작 순서로 담당자가 수행합니다. 실행 중인 로그를 임의로 지우지 않습니다. 이 작업 동안 외부 접속이 끊깁니다. 문제 원인 해결 없이 로그만 지우지 마세요.

```powershell
Stop-Service Cloudflared
Move-Item -LiteralPath "C:\MaisonCafe\logs\tunnel.log" -Destination "C:\MaisonCafe\logs\tunnel-보관할고유이름.log"
Start-Service Cloudflared
```
