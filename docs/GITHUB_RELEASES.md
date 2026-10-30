# GitHub Release → Windows 서버 반영

이 문서는 **소스/배포 파일 관리 절차**입니다. GitHub Actions에 카페 노트북 주소·터널 토큰·운영 DB를 등록하지 않고 실제 서버로 자동 배포하지 않습니다. 기존 `C:\MaisonCafe`와 공개 저장소를 따로 둡니다.

## 릴리스 담당자: 검증한 커밋을 버전으로 고정

1. 작업 브랜치 PR에서 변경 내용과 CI를 확인하고 `main`에 병합합니다. 실제 Windows 10 수동 검증 여부는 자동 테스트와 구분해 릴리스 노트에 적습니다.
2. 깨끗한 개발 clone에서 `main`을 갱신하고 공개 검수를 다시 실행합니다. 운영 DB는 clone 안에 두지 않습니다.

```bash
git switch main
git pull --ff-only
python tools/audit_public.py --history
# 해당 main 커밋의 GitHub CI 성공도 확인
# v1.4.0은 첫 공개 버전 예시이며, 이후에는 아직 사용하지 않은 실제 다음 버전을 선택
git tag -a v1.4.0 -m "First public V1.4 source and Windows operations package"
python tools/build_release.py --ref v1.4.0 --version v1.4.0 --out dist
git push origin v1.4.0
```

3. GitHub Release를 그 태그로 만들고 `dist/`의 Windows ZIP과 SHA256 파일을 함께 첨부합니다. 이전 버전·검사 결과·직접 검증하지 못한 항목을 적습니다. `gh`를 쓰면:

```bash
gh release create v1.4.0 dist/maison-de-gourmet-v1.4.0-windows.zip dist/maison-de-gourmet-v1.4.0-windows.sha256.txt --verify-tag --title "V1.4.0 — Windows 운영 준비" --notes-file 릴리스노트파일.md
```

버전 이름은 실제 발행할 값으로 바꾸고 이미 있는 태그/릴리스 파일은 덮어쓰지 않습니다. 이력은 현재 Git 도입 시점부터이며 V1.0~V1.3 태그를 소급 생성하지 않습니다. 태그가 참조하는 커밋이 `main`에 있고 CI가 성공했는지 확인합니다. 태그 push도 CI를 실행하지만 CI가 자동으로 배포/Release를 발행하지는 않습니다.

`build_release.py`는 선택한 **커밋된 Git tree**의 파일만 읽습니다. 작업 폴더의 수정 중 파일·추적되지 않은 DB·실제 설정·Git 인증정보는 복사하지 않습니다. 공개 검수를 통과한 파일로 ZIP과 manifest/전체 ZIP 해시를 생성하고 기존 출력 파일을 거부합니다. 설치 계약의 manifest `version`은 기존 스크립트 호환을 위해 `1.4`, 별도 `release_tag`는 `v1.4.0`, `source_commit`은 실제 커밋 SHA로 기록됩니다.

GitHub의 자동 생성 **Source code (zip)**은 설치 manifest가 없으므로 운영 설치 파일이 아닙니다. **첨부된 `maison-de-gourmet-vX.Y.Z-windows.zip`**을 사용합니다. SHA256은 파일 무결성 확인이며 전자서명이 아닙니다. 계정·저장소 주소와 태그/커밋/CI도 함께 확인하세요. [GitHub Release 안내](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

## Windows 설치 담당자: 업데이트 전

1. 기존 운영 폴더의 `Manage.cmd` → 상태를 확인하고 운영 설정·이벤트 ID·상품 ID를 보존합니다.
2. 새 릴리스 노트에서 운영 스크립트·의존성·스키마 변경 여부와 수동 검증 한계를 확인합니다. 이번 `Update.ps1`은 **운영 스크립트, 의존성 잠금, schema.sql이 같은 V1.4 앱 변경**만 지원합니다. 다른 운영 버전은 자동 업데이트로 강행하지 않습니다.
3. 기존 버전의 Windows ZIP도 안전한 설치 자료 폴더에 보관합니다. 코드만 롤백할 때 필요합니다. 고객 DB를 이전 백업으로 함께 되돌리는 절차가 아닙니다.
4. 현재 DB를 백업하고 최근 파일을 검증합니다.

```powershell
& "C:\MaisonCafe\scripts\Backup.ps1"
& "C:\MaisonCafe\scripts\Control-Server.ps1" -Action Status
```

외부 백업이 `partial`이면 원인을 해결하거나 담당자가 로컬 백업·외부 사본 상태를 확인한 뒤 판단합니다. 오류를 무시하고 자동 진행하지 않습니다. 복원 검증 메뉴로 별도 후보 DB를 확인할 수 있습니다.

## 다운로드·무결성·반영

GitHub Release의 해당 ZIP과 SHA256 파일을 `C:\Maison Releases` 같은 **운영 폴더 밖**에 내려받습니다. 관리자가 받은 파일의 출처와 해시를 확인합니다.

```powershell
$zip='C:\Maison Releases\maison-de-gourmet-v1.4.0-windows.zip'
$sum='C:\Maison Releases\maison-de-gourmet-v1.4.0-windows.sha256.txt'
$expected=((Get-Content -LiteralPath $sum -Raw).Trim() -split '\s+')[0]
$actual=(Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
if ($expected -notmatch '^[0-9a-fA-F]{64}$' -or $actual -ine $expected) { throw '해시 불일치: 설치 중단' }
# 파일 내용을 검토하고 Windows 출처 차단을 해제한 뒤 새 폴더로 압축 해제
Expand-Archive -LiteralPath $zip -DestinationPath 'C:\Maison Releases\v1.4.0'
& "C:\MaisonCafe\scripts\Update.ps1" -Package "C:\Maison Releases\v1.4.0\maison-cafe-v1.4"
```

처음 운영 환경을 만드는 경우에는 업데이트 대신 새 릴리스의 `Install.cmd`와 [설치 설명서](WINDOWS_INSTALL.md)를 사용합니다. 공개 Git clone에서 `git pull`한 파일을 `C:\MaisonCafe`에 통째로 덮어쓰지 않습니다.

업데이트기는 manifest → 후보 앱을 별도 SQLite 복제 DB에서 검사 → 서버 중지 → DB 안전 백업 → 이전 `app`을 `backups\code`에 보관 → 앱만 교체 → 다시 검사 → 이전에 실행 중이었으면 재시작 순서입니다. 실제 config/data/터널 토큰은 새 릴리스에서 복사하지 않습니다. 오류 시 자동 코드 복귀를 시도하고 점검 모드로 남기므로 로그를 확인해야 합니다.

## 이전 코드로 롤백

정상 실행되지만 앱 동작에 문제가 있어 이전 코드로 돌아갈 때:

1. 현재 DB를 다시 안전 백업하고 두 릴리스의 스키마/운영 스크립트가 호환되는지 확인합니다.
2. 보관한 이전 Release ZIP의 SHA256을 확인해 다른 폴더에 풉니다.
3. 동일한 `Update.ps1 -Package "이전릴리스\maison-cafe-v1.4"` 명령으로 **이전 앱 코드만** 반영합니다.
4. 서버 상태, 기존 고객·QR·복구·지급 완료 상태를 확인합니다. DB는 현재 상태를 유지해야 합니다.

첫 공개 버전에는 아직 이전 GitHub Release가 없습니다. 보관 중인 원래 V1.4 Windows ZIP을 호환 코드 복귀 후보로 사용할 수 있지만 실제 반영은 사전 검증이 필요합니다. `backups\code`의 폴더는 추가 복구 자료이며 고객 DB를 그 안에 합쳐 복사하지 않습니다.

DB 손상이 의심되면 코드 롤백과 별도로 [백업/복원 절차](BACKUP_RECOVERY.md)를 사용합니다. 과거 DB 복원은 백업 이후 적립·실물 보상 지급과 불일치할 수 있으므로 담당자가 내역을 대조해야 합니다.

## 실제 실행하지 않은 항목

카페 노트북 설치/업데이트/롤백·고정 도메인 공개는 이 저장소 준비 과정에서 수행하지 않았습니다. GitHub Windows hosted runner의 단위 테스트 성공도 Windows 10 현장 설치와 같지 않습니다. 실제 운영 전 [인수 검증표](WINDOWS_ACCEPTANCE.md)를 완료합니다.
