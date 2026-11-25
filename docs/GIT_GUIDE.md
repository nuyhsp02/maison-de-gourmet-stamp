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
python tools/audit_public.py --staged
git diff --cached --stat
git diff --cached
git commit -m "feat: 구체적인 변경"
```

최종 공개·PR·병합은 아래 단일 브랜치 검수 및 사용자 승인 절차를 따릅니다. 등록된 보호 규칙은 PR/필수 검사/최신 main 반영을 요구하며 force push/브랜치 삭제를 막도록 설정합니다. 실제 적용 여부는 GitHub Settings → Branches에서 확인합니다. 저장소 보호 규칙은 소스 파일만으로 자동 적용되지 않으므로 초기 공개 시 별도 API 설정 결과를 기록합니다. [GitHub 공식 보호 브랜치 안내](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

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
node --check app/static/admin.js
node --test app/tests/scanner.test.cjs app/tests/app-flow.test.cjs app/tests/admin-flow.test.cjs
& .\tests\Check-PowerShell.ps1
& .\.test-venv\Scripts\python.exe tools/audit_public.py --history
```

HTTP 테스트는 localhost의 임시 서버와 임시 DB만 사용합니다. 환경변수 `MAISON_HTTP_TESTS`가 없으면 2개가 skip됩니다. Node는 개발/CI 검사에만 필요하며 Windows 운영 설치의 필수 프로그램이 아닙니다.

## 공개 전에 확인

`.gitignore`는 **이미 추적한 파일을 숨기지 않습니다**. `git status`와 `git ls-files`를 함께 확인합니다. 실제 DB/백업/로그/개인 설정/QR 내보내기/인증서/토큰 파일을 커밋하지 않습니다. `.env`의 값, 개인 복구 링크, 운영 QR 전체 URL을 이슈/PR/README/스크린샷에도 붙이지 않습니다.

`app/event.json`은 기존 앱 테스트와 `--dev`를 위한 draft 예제이며 실제 운영 이벤트 파일이 아닙니다. 실제 파일은 무시되는 `config/event.json`으로 관리합니다. 공용 상품 ID는 비밀 QR 키가 아닙니다. decoder fixture의 반복 문자 QR은 테스트 전용입니다.

`tools/audit_public.py`는 현재 추적 파일/스테이징과 도달 가능한 전체 이력에서 금지 경로·SQLite 내용·대표 토큰/키 형식·QR 리터럴·예제 설정을 검사합니다. 매칭 원문을 출력하지 않습니다. 모든 형태의 비밀을 찾는 완전한 보안 제품은 아니므로 수동 검토도 필요합니다. 실제 데이터와의 대조 결과는 [공개 검수 기록](PUBLIC_REVIEW.md)을 참고하세요.

`--staged`는 인덱스와 다음 커밋의 작성자·커미터를, `--ref`는 지정한 브랜치의 전체 조상을, `--history`는 모든 참조와 주석 태그를 진단합니다. 커밋 전후에 각각 사용할 수 있습니다. 비공개 보관 참조가 있는 이 프로젝트의 전송 전 검사는 아래 `--publication` 절차로 진행합니다. [첫 이력 검수](devlog/DEV-014-public-history-review.md) · [후속 이력 검수](devlog/DEV-015-public-history-and-asset-permission.md)

### 공개 후보와 비공개 보관 이력을 함께 쓰는 경우

1. **백업.** 비공개 장치에 전체 참조 bundle, 브랜치·HEAD·원격 정보, 미커밋 패치와 신규 파일 원본을 보관합니다. bundle 검증과 복원 대조를 마칩니다. 문서·도구를 최종 커밋한 후에는 그 SHA를 포함하는 bundle을 별도 보관합니다.
2. **승인 대상.** 공개 저장소와 대상 브랜치 한 개를 특정합니다. 자산 허가 범위, 상대 링크, 실제 이미지, 전화번호 형태가 있는 합성 시험 파일과 두 커밋의 변경 목록을 검토합니다. 검토 기록에 전체 SHA를 남깁니다.
3. **SHA 고정.** `APPROVED_SHA`에는 검토 기록의 전체 SHA를 넣습니다. `PUBLIC_MAIN_SHA`와 `PUBLIC_BASE_SHA`에는 독립적으로 확인한 원격 main과 기존 PR #4 HEAD의 전체 SHA를 넣습니다. 브랜치가 움직였으면 새 후보로 다시 검토합니다.
4. **보호된 대조 자료.** `REVIEW_DIR`은 저장소 밖의 소유자 전용 디렉터리(0700)입니다. `private-values.txt`(0600)는 한 줄에 하나씩 둔 중복 없는 비공개 비교값이고 `reference.sha256`은 출처와 목록을 대조했을 때 별도로 보존한 원본 바이트의 SHA-256입니다. `EXPECTED_PRIVATE_COUNT`도 그 검토 기록에서 가져옵니다. 이번 목록은 7개였습니다. 실행 직전에 변경된 파일로 기대 지문·개수를 새로 계산하면 검토 기록과의 비교가 무의미해집니다. 값은 표준 입력으로 전달하며 원본을 유지합니다.
5. **검수.** 아래 절차는 깨끗한 최종 작업 트리에서 검수 테스트, Git 공백 검사, 후보의 모든 커밋·파일 버전·메타데이터 검사, 알려진 비공개 값 대조를 차례로 실행합니다. 보호 자료가 없거나 출처를 확인할 수 없으면 그 범위는 미확인으로 남기고 전송을 중단합니다. 보관 참조의 발견 건은 별도로 검토한 전체 객체 ID·가린 파일명·탐지 규칙 목록의 지문과 일치하고, 전부 후보 밖에 있어야 합니다. 이번 검토의 지문은 `archive-review.sha256`에 보관했습니다. 기존에 발견 건이 없는 저장소는 이 파일을 생략할 수 있습니다. 새로운 발견 건이 생기면 전체 참조 진단을 검토합니다. 실패 결과에 표시된 지문을 그대로 승인값으로 복사하지 않습니다.
6. **원격 경계 확인.** 전송 직전 원격 main과 PR #4 HEAD를 승인 기록과 대조합니다. 예상 밖 원격 주소·추가 push URL·기존 대상 브랜치, Git 오류, JSON 결과 누락, SHA 변동, 검사의 실패·중단은 모두 중단 사유입니다.
7. **승인된 단일 브랜치 전송.** 아래는 최초 공개를 위한 Bash 절차입니다. 기본 모드 `check`는 검사에서 끝납니다. 최종 사용자 승인 후에만 `PUBLICATION_ACTION=push`로 실행합니다. 테스트와 검사를 포함한 블록 전체를 실행합니다. 전송 시 `--force-with-lease="$ref:"`의 빈 기대값은 대상 브랜치가 원격에 여전히 없다는 조건입니다. 확인 직후 같은 이름의 브랜치가 생겨도 덮어쓸 수 없습니다. `git push --all`, `--mirror`, 기존 참조 강제 덮어쓰기 및 태그 전송은 사용하지 않습니다.

```bash
bash <<'PUBLISH'
set -euo pipefail
umask 077
: "${APPROVED_SHA:?}" "${PUBLIC_MAIN_SHA:?}" "${PUBLIC_BASE_SHA:?}" "${BACKUP_BUNDLE:?}"
: "${REVIEW_DIR:?}" "${EXPECTED_PRIVATE_COUNT:?}"
ref=refs/heads/feature/contact-privacy-admin
remote=https://github.com/nuyhsp02/maison-de-gourmet-stamp.git
action=${PUBLICATION_ACTION:-check}
[[ "$action" == check || "$action" == push ]] || exit 1
branch=$(git symbolic-ref HEAD)
tip=$(git rev-parse HEAD)
[[ "$branch" == "$ref" && "$tip" == "$APPROVED_SHA" ]] || exit 1
changes=$(git status --porcelain=v1 -uall)
[[ -z "$changes" ]] || exit 1
git bundle verify "$BACKUP_BUNDLE"
backup_tip=$(git bundle list-heads "$BACKUP_BUNDLE" "$ref")
[[ "$backup_tip" == "$APPROVED_SHA $ref" ]] || exit 1
git diff --check "$PUBLIC_MAIN_SHA...$APPROVED_SHA"
python -B -m pytest -q -p no:cacheprovider tests/test_repository.py
python -c '
import os, stat, sys
from pathlib import Path
try:
    d = Path(sys.argv[1]).resolve(strict=True)
    repo = Path.cwd().resolve()
    entries = [(d, 0o700), (d / "private-values.txt", 0o600), (d / "reference.sha256", 0o600)]
    archive = d / "archive-review.sha256"
    if archive.exists() or archive.is_symlink():
        entries.append((archive, 0o600))
    ok = repo != d and repo not in d.parents and d.is_dir()
    for path, mode in entries:
        s = path.lstat()
        ok = ok and s.st_uid == os.getuid() and stat.S_IMODE(s.st_mode) == mode
        ok = ok and (path == d or stat.S_ISREG(s.st_mode))
except (OSError, RuntimeError):
    ok = False
sys.exit(0 if ok else "Protected reference storage check failed")
' "$REVIEW_DIR"
reference_digest=$(cat "$REVIEW_DIR/reference.sha256")
audit_args=(--publication --ref "$ref" --expected-sha "$APPROVED_SHA" --public-base "$PUBLIC_BASE_SHA")
if [[ -e "$REVIEW_DIR/archive-review.sha256" ]]; then
    archive_digest=$(cat "$REVIEW_DIR/archive-review.sha256")
    audit_args+=(--reviewed-archive-digest "$archive_digest")
fi
report=$(python -B tools/audit_public.py "${audit_args[@]}" \
    --private-values-stdin --private-values-sha256 "$reference_digest" --expected-private-count "$EXPECTED_PRIVATE_COUNT" \
    < "$REVIEW_DIR/private-values.txt")
printf '%s\n' "$report" | python -c '
import json, sys
r = json.load(sys.stdin)
sys.exit(0 if (r["scope"] == "publication gate" and r["result"] == "pass"
    and r["candidate_ref"] == sys.argv[1] and r["candidate_commit"] == sys.argv[2]
    and r["public_base"] == sys.argv[3] and r["private_values_checked"] == int(sys.argv[4])
    and r["archive_review"]["result"] == "pass") else 1)
' "$ref" "$APPROVED_SHA" "$PUBLIC_BASE_SHA" "$EXPECTED_PRIVATE_COUNT"
if [[ "$action" == check ]]; then
    printf 'Local review passed for %s\n' "$APPROVED_SHA"
    exit 0
fi
fetch_url=$(git remote get-url --all origin)
push_url=$(git remote get-url --push --all origin)
[[ "$fetch_url" == "$remote" && "$push_url" == "$remote" ]] || exit 1
remote_oid() { git ls-remote --exit-code origin "$1" | awk '{print $1}'; }
remote_main=$(remote_oid refs/heads/main)
remote_base=$(remote_oid refs/pull/4/head)
[[ "$remote_main" == "$PUBLIC_MAIN_SHA" && "$remote_base" == "$PUBLIC_BASE_SHA" ]] || exit 1
git merge-base --is-ancestor "$PUBLIC_MAIN_SHA" "$PUBLIC_BASE_SHA"
existing=$(git ls-remote origin "$ref")
tip=$(git rev-parse "$ref")
[[ -z "$existing" && "$tip" == "$APPROVED_SHA" ]] || exit 1
git -c remote.origin.mirror=false push --no-follow-tags --recurse-submodules=no --force-with-lease="$ref:" origin "$APPROVED_SHA:$ref"
published=$(remote_oid "$ref")
[[ "$published" == "$APPROVED_SHA" ]] || exit 1
PUBLISH
```

8. **전송 후 확인.** 마지막 원격 SHA 대조가 실패하면 전송 결과를 미확인으로 기록하고 PR 작업을 중단합니다. 통신이 끊겼으면 원격 브랜치를 읽어 실제 SHA부터 확인합니다.
9. **Draft PR.** 원격 SHA 일치와 PR 생성 승인을 확인한 뒤 해당 브랜치로 새 Draft PR을 만듭니다. 기존 PR #4는 보존합니다.
10. **병합 승인.** PR의 HEAD가 검수 기록과 일치하고 필수 CI가 통과했는지 확인합니다. 사용자의 최종 병합 승인을 받은 후에만 병합합니다.

비공개 값 대조의 범위와 이번 오류 출력 시험은 [DEV-016](devlog/DEV-016-publication-fail-closed.md)에 기록했습니다. 평문 고객 데이터, 키 보관소 또는 운영 서버를 열어 목록을 늘리는 절차는 포함하지 않습니다.

## 줄바꿈과 작성자

`.gitattributes`는 기존 Windows 스크립트의 UTF-8 BOM/CRLF 바이트를 그대로 보존합니다. V1.4 업데이트기가 운영 스크립트의 SHA256을 비교하므로 Git이 자동으로 바꾸지 않도록 한 설정입니다. 커밋의 소유자 정보는 실제 GitHub 계정과 noreply 이메일을 사용합니다. 다른 프로젝트의 전역 Git 설정은 변경하지 않습니다.

## 버전

태그/Release 발행 절차는 [GITHUB_RELEASES.md](GITHUB_RELEASES.md)를 따릅니다. `main`의 검증된 커밋만 태그로 고정하고, 이미 공개한 태그를 다른 커밋으로 이동하지 않습니다. 현재 첫 공개 버전보다 과거 버전의 Git 태그를 허위로 추가하지 않습니다.
