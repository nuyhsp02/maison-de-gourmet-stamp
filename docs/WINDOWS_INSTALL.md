# Windows 설치 설명서 — 설치 담당자용

Python 3.15.2의 Windows CI 검증을 추가했으나 실제 카페 Windows 10 노트북의 현장 검증은 아직 하지 않았습니다. 카페 고객을 받기 전에 [WINDOWS_ACCEPTANCE.md](WINDOWS_ACCEPTANCE.md)의 격리 시험·제안 기준 확정·실기기 인수를 완료하세요. 이 문서의 PowerShell은 **64비트 Windows PowerShell 5.1 관리자 창**에서 실행합니다. 기본 경로 예시이며 공백 경로는 따옴표를 유지하세요. 패키지는 `C:\Maison Package\maison-cafe-v1.4`처럼 설치 폴더 밖에 풉니다.

## 1. 미리 준비

- Windows 10 22H2 x64, 빌드 19045 이상, 보안 업데이트 제공 상태 확인. 일반 지원 종료 및 연장 지원 자격은 Microsoft 안내를 확인합니다. 관리자 계정/승인이 필요합니다.
- 카페의 업무용 Windows 10은 [Microsoft 업무용 ESU](https://learn.microsoft.com/en-us/windows/whats-new/extended-security-updates) 적용·활성 상태와 최근 보안 업데이트 수신을 별도로 확인합니다. 개인용 무료 ESU가 카페 서버에 적용된다고 가정하지 않습니다. 구체적 증거와 운영 보류 조건은 [현장 인수 준비](WINDOWS_ACCEPTANCE.md#1-격리와-실행-전-준비)를 사용합니다. 설치기는 ESU를 등록하거나 실제 수신 상태를 자동 보증하지 않습니다.
- 운영 후보는 **공식 CPython 3.15.2 x64 일반 GIL**입니다. Windows 10 이상 지원은 [공식 3.15 문서](https://docs.python.org/3.15/using/windows.html)에 따릅니다. 기존 3.12/3.13 앱 호환성 검사는 유지하지만 현재 관측된 SQLite 3.49.1/3.50.4는 운영 사전 검사에서 거부됩니다. Python 번호만으로 엔진 안전성을 판단하지 않습니다.
- Python 3.15 설치 관리자의 기본 런타임은 사용자 전용입니다. SYSTEM 작업에는 관리자 소유 공유 경로의 전체 런타임이 필요합니다. 이 작업에서 직접 검증한 공급은 [공식 문서의 CPython NuGet 패키지](https://docs.python.org/3.15/using/windows.html#the-nuget-org-packages) `python` 3.15.2입니다. 사용자용 Store/Conda/free-threaded 환경이나 DLL만 섞는 방법은 사용하지 않습니다. 공유 런타임에는 SYSTEM 읽기/실행, 관리자만 쓰기 권한이 있어야 합니다.
- 인터넷 연결(Python 패키지 설치, 이후 Cloudflare 통신), 로컬 NTFS 디스크 여유, 전원 연결. 동기화 폴더/네트워크/USB에는 **운영 DB**를 두지 않습니다. 기본 경로를 권장하며 깊은 긴 경로는 피합니다.
- ZIP 출처와 내용을 확인합니다. Windows 파일 속성의 차단 해제를 한 뒤 압축 해제합니다. 전역 실행 정책을 Unrestricted/Bypass로 바꾸지 않습니다. 조직 정책이 스크립트를 차단하면 담당자가 서명/승인해야 합니다. 스크립트는 서명되지 않았고 SHA256 목록은 전송 손상 확인용이며 출처 인증 서명이 아닙니다.

### 검증된 전체 런타임을 별도 경로에 준비

개발/설치 담당자가 관리자 창에서 공식 NuGet 도구를 준비한 후, **비어 있는 새 공유 폴더**에 다음과 같이 받습니다. 기존 Python/venv 경로에 덮어쓰지 않습니다. 이는 [공식 CPython 배포](https://www.nuget.org/packages/python/3.15.2) 전체를 받는 명령이며 DLL 교체가 아닙니다.

```powershell
nuget.exe install python -Version 3.15.2 -OutputDirectory "C:\MaisonPython3152" -Source https://api.nuget.org/v3/index.json -NonInteractive
& "C:\MaisonPython3152\python.3.15.2\tools\python.exe" "C:\Maison Package\maison-cafe-v1.4\tools\check_sqlite_runtime.py" --require-fixed
```

NuGet 도구 출처는 위 Python 공식 문서의 Microsoft 다운로드 안내를 사용합니다. 런타임은 자동 등록되지 않으므로 아래 `-PythonExe`를 명시합니다. 공식 install manager의 `--target` 공유 경로 방식도 문서에 있으나 이 CI는 NuGet 경로를 검증했습니다. 어느 공급이든 실제 엔진/source ID와 GIL/아키텍처를 재확인해야 합니다.

기존 3.12/3.13 venv는 자동 변환하지 않습니다. 서버 중지·DB 백업·복원 확인 후, 담당자가 별도 시험 경로에 새 런타임/venv를 검증하는 계획을 세워야 합니다. `Install.ps1` 재실행이나 코드 업데이트가 Python 마이그레이션을 대신하지 않습니다. 기존 DB/설정/venv 삭제는 이 안내에 포함되지 않습니다.

## 2. 준비 설치 (도메인·기간 미정일 때 권장)

`Install.cmd` 더블클릭 → UAC 관리자 승인 → 도메인/확정 이벤트 경로 질문은 Enter로 비웁니다. 또는 아래 한 명령으로 질문 없이 준비만 합니다.

```powershell
& "C:\Maison Package\maison-cafe-v1.4\scripts\Install.ps1" -PrepareOnly
```

경로/Python을 직접 지정해야 할 때:

```powershell
& "C:\Maison Package\maison-cafe-v1.4\scripts\Install.ps1" -Root "C:\Maison Cafe" -PythonExe "C:\MaisonPython3152\python.3.15.2\tools\python.exe" -PrepareOnly
```

설치기는 OS/Python·패키지 체크섬·선택한 SQLite 사전 확인 → 전용 폴더/ACL → 가상환경 → 기존 venv 버전/빌드·SQLite 확인 → wheel-only 고정 의존성 설치·검사 순으로 진행합니다. 미확정 도메인 또는 실제 event.json 부재 시 **DB 생성·서버 실행·예약 작업 등록을 하지 않고** 준비 상태로 끝납니다. 오류 시 열린 창의 안내를 확인합니다. 기존 고객 DB나 설정을 지워서 재시도하지 마세요.

반복 설치는 설치 표식과 체크섬을 확인하고 기존 파일을 보존합니다. DB가 있는 설치에서는 의존성을 다시 설치하지 않고 검사만 합니다. 코드/의존성이 손상되거나 버전이 다르면 덮어쓰지 않고 중단합니다. 설치 도중 파일 복사 이전에 실패해 표식이 없는 비어 있지 않은 폴더가 남았다면, **고객 DB가 없는 새 준비 폴더임을 확인한 후** 다른 빈 폴더로 다시 설치하세요. 기존 운영 폴더를 삭제하지 마세요.

## 3. 실제 설정 확정

`C:\MaisonCafe\config\operations.json`의 값을 담당자가 편집합니다. 비밀정보는 넣지 않습니다.

```json
{
  "PublicUrl": "https://stamps.example.com",
  "Port": 8765,
  "MetricsPort": 20241,
  "ExternalBackupPaths": ["E:\\CafeBackup"],
  "DailyBackupTime": "03:00",
  "MinimumFreeMB": 256,
  "Version": "1.4"
}
```

위 도메인은 예시입니다. **소유한 실제 고정 도메인**을 지정해야 합니다. 끝 슬래시/경로/포트/대문자/사용자정보를 넣지 않습니다. Port/MetricsPort는 서로 다른 1024~65535이고 실제 충돌 여부는 서버 시작으로 확인합니다. 예약 시간은 Windows 현지 시간입니다. 카페 시간대·자동 시간 동기화를 확인하세요. 백업 시간 변경 후에는 자동 실행 등록을 다시 해야 합니다.

실제 `event.json`을 `config\event.json`으로 준비합니다. 기존 앱의 JSON 읽기 방식에 맞춰 UTF-8(BOM 없이)로 저장합니다. Windows PowerShell 5.1의 `Set-Content -Encoding UTF8`은 BOM을 넣으므로 이벤트 편집에 쓰지 마세요. 확정된 기존 파일이 있으면 그것을 복사하고, 없으면 `config\event.example.json`의 복사본을 검토해 만듭니다. 설치기는 기존 `config\event.json`을 덮어쓰지 않습니다. `app\event.json`은 런타임에 사용하지 않습니다.

- 이벤트 ID와 상품 10개 ID는 **기존 DB와 동일하게 유지**합니다. 상품명/기간/보상은 운영자가 확정합니다.
- `draft`를 false로 확정하고 `starts_at`, `ends_at`, `reward_until`을 시간대 포함 ISO8601로 설정합니다(예: `2026-12-01T00:00:00+09:00`은 형식 예시이며 실제 기간은 직접 결정).
- 기존 앱 제한: 시작 < 종료, 이벤트 최대 32일, 보상 기한은 종료 이상. 상품은 시즌 4종+지정 6종이며 ID/카테고리/아이콘 구조를 유지합니다.
- 설정 예제에는 미확정 값이 남아 있습니다. 설치기가 임의로 실제 상품·기간·운영 인증정보를 만들어 넣지 않습니다.

## 4. 기존 고객 DB 이관 (있는 경우만, 새 DB 생성 전에)

이관이 필요 없으면 5번으로 갑니다. 기존 고객을 유지하려면 원래 사용한 이벤트 파일도 함께 확인해야 합니다.

1. 기존 서버의 실제 DB 위치를 확인합니다. 기존 고객 적립을 멈추고 아래 기존 명령으로 **새 SQLite 백업 파일**을 만듭니다. 운영 중인 `.sqlite` 파일만 탐색기로 복사하지 않습니다. WAL에 아직 저장된 데이터가 빠질 수 있습니다.

```text
기존 환경 Python manage.py --config 실제이벤트경로 --database 실제DB경로 backup 새백업경로
```

2. 백업 파일을 안전한 매체로 Windows에 옮깁니다. 기존 서버는 중지 상태를 유지해 고객 기록이 양쪽으로 나뉘지 않게 합니다. 기존 원본은 지우지 않습니다.
3. 준비 설치 + 실제 event.json/operations.json 설정까지 완료하고, **대상 `data\collection.sqlite`가 없는 상태**에서 가져옵니다.

```powershell
& "C:\MaisonCafe\scripts\Import-Database.ps1" -BackupFile "E:\이관\collection-safe.backup"
```

SQLite 백업 API로 새 DB를 만들고 무결성·외래키·이벤트 서명을 검사합니다. 기존 대상 DB가 있으면 거부합니다. 이미 빈 DB를 생성했다면 삭제하거나 강제 가져오지 말고 복원 검증/활성화 절차로 처리합니다. 쿠키 호스트가 같은 경우 기존 세션, 복구 토큰, 상품 QR 키, 지급 기록이 보존됩니다. 도메인 변경 시 쿠키 자동 이전은 되지 않으며 예전 QR의 호스트 변경·재출력 및 고객 복구 안내가 필요합니다.

## 5. 설치 마무리 및 자동 실행

처음에 압축을 푼 **원본 패키지의 Install.cmd**를 다시 실행합니다(운영 설치 폴더 자신을 패키지 원본으로 사용하지 않음). 사용자 정의 Root를 썼으면 원래와 같은 `-Root` 인자를 사용합니다. 현재 설정을 보존한 채 DB를 읽기 전용 검사하거나 새 DB만 초기화하고, 별도 복제 DB로 로컬 Waitress HTTP 검사를 합니다. 검사 포트는 임시 포트이며 운영 포트의 충돌은 실제 시작 때 확인됩니다.

자동 실행 등록 질문에 Y를 입력하면 서버 및 매일 백업 예약 작업을 SYSTEM 계정으로 등록하고 시작합니다. 또는:

```powershell
& "C:\MaisonCafe\scripts\Register-Autostart.ps1" -StartNow
```

등록 내용:

- `MaisonCafe-Server-설치경로해시`: 부팅 트리거, SYSTEM/ServiceAccount/Highest, 작업 디렉터리 명시, 중복 무시, 실행 시간 제한 없음, 감독 오류 시 1분 후 재시도(999회 설정).
- `MaisonCafe-Backup-설치경로해시`: 매일 현지 시간 지정 시각, SYSTEM, 미실행 회차 재실행, 가능하면 절전 해제, 실패 시 15분 간격 3회 재시도, 최대 1시간 제한.
- Waitress는 127.0.0.1 전용, 디버그 비활성화. 감독 프로세스가 Waitress 종료 후 10초 대기 재시도. 프로세스가 살아 있으나 응답하지 않는 종류의 장애는 자동 강제 종료하지 않으므로 상태 확인 후 재시작해야 합니다.
- 실제 Windows 재부팅/로그인 없이 실행, 절전·배터리·네트워크 복구는 실기기에서 확인해야 합니다. `RestartCount`는 무한 횟수가 아니며 반복 실패 원인을 해결해야 합니다.

작업 해제(데이터는 유지):

```powershell
& "C:\MaisonCafe\scripts\Unregister-Autostart.ps1"
```

서버/백업 작업만 해제합니다. Cloudflare Windows 서비스는 별도입니다. 서버 중지 메뉴는 자동 실행 작업을 비활성화하고 점검 플래그를 남깁니다. **그 상태로 재부팅해도 켜지지 않습니다.** 반드시 시작 메뉴로 점검 모드를 해제하세요.

## 6. 백업·복원·업데이트

[BACKUP_RECOVERY.md](BACKUP_RECOVERY.md)를 따릅니다. 백업 파일 역시 고객 식별 정보이므로 공유 금지입니다. 외부 백업은 NTFS USB 전용 폴더를 권장합니다. FAT/exFAT처럼 ACL을 적용할 수 없는 위치는 건너뛰고 실패 처리합니다. UNC 공유는 SYSTEM 컴퓨터 계정/서버 측 권한과 SQLite 접근을 별도로 검증해야 하며, 로그인 사용자의 매핑 드라이브/비밀번호를 예약 작업에 자동 전달하지 않습니다.

운영 코드 업데이트:

```powershell
& "C:\MaisonCafe\scripts\Update.ps1" -Package "C:\새 패키지\maison-cafe-v1.4"
```

운영 스크립트/의존성 잠금 파일/schema.sql이 정확히 같은 V1.4 **앱만 변경된 패키지**에 한합니다. 체크섬 → 후보 앱을 별도 DB에서 검사 → 서버 중지 → 안전 백업 → 이전 앱 보관 → 교체 후 검사 → 원래 실행 중이었으면 시작합니다. 설정/운영 DB는 복사하지 않습니다. 실패 시 이전 앱으로 되돌리고 점검 모드에서 중단합니다. 다른 런타임·운영 스크립트·스키마 변경이 필요한 차기 버전은 이 업데이트기로 강행하지 말고 별도로 검토하세요. 구형 V1.3 폴더를 그대로 통째로 덮어쓰는 방식은 지원하지 않습니다.

## 7. 전원 및 일상 운영

전원 어댑터를 연결하고 운영 시간에는 절전/최대 절전·덮개 닫을 때 절전을 사용하지 않도록 Windows 설정을 직접 확인합니다. 화면 꺼짐은 허용해도 됩니다. 자동 업데이트 재부팅 시간은 영업 외로 조정합니다. 전원을 꺼둔 동안은 고객 LTE/5G에서도 접속할 수 없습니다. Wi-Fi 자격 증명이 로그인 전에도 연결되는지 검증합니다. BitLocker 부팅 PIN/복구 화면이 있으면 사람이 입력하기 전에는 무인 서버가 시작되지 않을 수 있습니다. 보안 설정을 임의로 해제하지 말고 운영 절차에 반영하세요.

## 참고 근거

[Microsoft 예약 작업 설정](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset?view=windowsserver2022-ps), [SYSTEM 예약 작업 Principal](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2022-ps), [SQLite 백업 API](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup).


## Python 3.15.2 검증 범위

[실제 런타임 및 반복 검증 기록](devlog/DEV-008-python315-runtime.md)을 확인하세요. Run-Server.ps1도 Waitress 실행 전 실제 venv의 SQLite 수정 범위를 검사합니다. 사전 검사가 실패하면 서버를 시작하지 않습니다. 기존 15초/SQLite 내구성 설정은 바꾸지 않습니다. 정식 새 릴리스는 아직 만들지 않았으며 v1.4.0의 다운로드 파일이 이 변경을 포함한다고 간주하지 마세요.
