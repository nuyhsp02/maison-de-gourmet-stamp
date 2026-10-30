# Git 사용 안내

저장소: https://github.com/nuyhsp02/maison-de-gourmet-stamp

V1.4 실제 소스에서 Git 관리를 시작했습니다. 이전 앱/DB/개발 ZIP에 Git 이력이 없었으므로 현재 검수한 소스의 첫 커밋과 그 이후 작업만 기록합니다. 과거 날짜의 커밋/가짜 버전 태그를 만들지 않습니다. 기존 V1.4 배포 ZIP과 실제 고객 DB는 별도로 보존합니다.

## 브랜치와 작업

- `main`: CI가 통과한 변경을 PR로 병합합니다.
- `feature/설명`, `fix/설명`, `chore/설명`, `docs/설명`: 실제 작업별 짧은 브랜치. 작업하지 않은 빈 브랜치를 예시용으로 만들지 않습니다.
- 초기 공개 준비도 `chore/github-publication` 브랜치에서 실제 변경을 기록합니다.

```bash
git clone https://github.com/nuyhsp02/maison-de-gourmet-stamp.git
cd maison-de-gourmet-stamp
git switch main
git pull --ff-only
git switch -c feature/실제-작업명
# 수정과 테스트
git status --short
git add 수정한파일
python tools/audit_public.py --staged --history
git diff --cached --stat
git diff --cached
git commit -m "feat: 구체적인 변경"
git push -u origin feature/실제-작업명
```

GitHub에서 PR → 변경 파일 확인 → `Required checks` 통과 → 병합합니다. 등록된 보호 규칙은 PR/필수 검사/최신 main 반영을 요구하며 force push/브랜치 삭제를 막도록 설정합니다. 실제 적용 여부는 GitHub Settings → Branches에서 확인합니다. 저장소 보호 규칙은 소스 파일만으로 자동 적용되지 않으므로 초기 공개 시 별도 API 설정 결과를 기록합니다. [GitHub 공식 보호 브랜치 안내](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

개인 저장소는 본인 PR에 본인 승인을 할 수 없으므로 승인 인원은 0으로 두고 PR/CI 통과를 필수로 사용합니다. 실제 협업자가 생기면 검토 승인 정책을 조정합니다. CI를 무시하는 관리자 병합/force push를 기본 절차로 쓰지 않습니다.

## 테스트

Python 3.12/3.13 일반 CPython, Node 24를 CI 기준으로 사용합니다. 운영 가상환경 대신 별도 개발 환경에서 실행하세요.

Windows PowerShell:

```powershell
py -3.13 -m venv .test-venv
& .\.test-venv\Scripts\python.exe -m pip install -r requirements-test.lock
$env:PYTHONUTF8='1'
$env:MAISON_HTTP_TESTS='1'
& .\.test-venv\Scripts\python.exe -m pytest -q app/tests tests
node --check app/static/app.js
node --test app/tests/scanner.test.cjs app/tests/app-flow.test.cjs
& .\tests\Check-PowerShell.ps1
& .\.test-venv\Scripts\python.exe tools/audit_public.py --history
```

HTTP 테스트는 localhost의 임시 서버와 임시 DB만 사용합니다. 환경변수 `MAISON_HTTP_TESTS`가 없으면 2개가 skip됩니다. Node는 개발/CI 검사에만 필요하며 Windows 운영 설치의 필수 프로그램이 아닙니다.

## 공개 전에 확인

`.gitignore`는 **이미 추적한 파일을 숨기지 않습니다**. `git status`와 `git ls-files`를 함께 확인합니다. 실제 DB/백업/로그/개인 설정/QR 내보내기/인증서/토큰 파일을 커밋하지 않습니다. `.env`의 값, 개인 복구 링크, 운영 QR 전체 URL을 이슈/PR/README/스크린샷에도 붙이지 않습니다.

`app/event.json`은 기존 앱 테스트와 `--dev`를 위한 draft 예제이며 실제 운영 이벤트 파일이 아닙니다. 실제 파일은 무시되는 `config/event.json`으로 관리합니다. 공용 상품 ID는 비밀 QR 키가 아닙니다. decoder fixture의 반복 문자 QR은 테스트 전용입니다.

`tools/audit_public.py`는 현재 추적 파일/스테이징과 도달 가능한 전체 이력에서 금지 경로·SQLite 내용·대표 토큰/키 형식·QR 리터럴·예제 설정을 검사합니다. 매칭 원문을 출력하지 않습니다. 모든 형태의 비밀을 찾는 완전한 보안 제품은 아니므로 수동 검토도 필요합니다. 실제 데이터와의 대조 결과는 [공개 검수 기록](PUBLIC_REVIEW.md)을 참고하세요.

민감정보를 잘못 커밋했다면 push 전에 중단하세요. 이미 공개했다면 파일을 다음 커밋에서 삭제하는 것만으로 해결되지 않습니다. 토큰 폐기/회전과 영향을 받은 QR·복구 링크의 대응을 먼저 결정하고 이력 정리는 담당자가 별도 검토합니다. 기존 Git 이력을 임의로 강제 재작성하지 않습니다.

## 줄바꿈과 작성자

`.gitattributes`는 기존 Windows 스크립트의 UTF-8 BOM/CRLF 바이트를 그대로 보존합니다. V1.4 업데이트기가 운영 스크립트의 SHA256을 비교하므로 Git이 자동으로 바꾸지 않도록 한 설정입니다. 커밋의 소유자 정보는 실제 GitHub 계정과 noreply 이메일을 사용합니다. 다른 프로젝트의 전역 Git 설정은 변경하지 않습니다.

## 버전

태그/Release 발행 절차는 [GITHUB_RELEASES.md](GITHUB_RELEASES.md)를 따릅니다. `main`의 검증된 커밋만 태그로 고정하고, 이미 공개한 태그를 다른 커밋으로 이동하지 않습니다. 현재 첫 공개 버전보다 과거 버전의 Git 태그를 허위로 추가하지 않습니다.
