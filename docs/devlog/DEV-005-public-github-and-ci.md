# DEV-005 · Public GitHub 저장소와 CI 도입

- 기록 유형: Git·PR·Release·CI 기록 기반 회고.
- 확인 가능한 도입일: **2026-10-30**. 아래 커밋/PR/Release로 대조.
- 실행 주체: AI 지원 저장소 정비/검수·Git 도구 실행과 GitHub CI. 커밋 작성자 정보만으로 소유자의 수작업 구현 비율/코드 이해 수준을 추론하지 않음.

## 1. 문제와 예상 동작

현재 소스를 개인 Public 저장소와 포트폴리오로 관리하되 운영 DB/설정/QR/토큰을 올리지 않아야 합니다. 과거 버전의 Git 이력을 꾸미지 않고 작업 브랜치·PR·필수 CI·태그/Release로 검증 결과를 연결해야 합니다.

## 2. 확인한 배경

[Git 사용 안내](../GIT_GUIDE.md)와 [공개 검수 기록](../PUBLIC_REVIEW.md)은 기존 이력이 없어 현재 V1.4에서 시작한 사실을 기록합니다. 과거 V1.0~V1.3 변경 문서가 있다고 해당 버전의 Git 커밋이 있었다고 해석하지 않습니다. 저장소에 비밀이 유출된 사고를 해결한 사례도 아닙니다.

## 3. 진행 과정

1. [1c23cfa](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1c23cfa023785632b801a0055d116bcfb55b6807): 검수한 V1.4를 운영 데이터 없이 반입.
2. [e61024e](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/e61024e0ce91f6aa524332bebcac5b23ec8268e0): 공개 문서/CI/릴리스 도구 추가.
3. 작업 브랜치와 [PR #1](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/1)에서 Windows 실패를 확인하고 [별도 로그](DEV-003-windows-sqlite-ci.md)의 수정을 반영.
4. [1284c52](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f)로 병합 후 main CI 확인, [v1.4.0 Release](https://github.com/nuyhsp02/maison-de-gourmet-stamp/releases/tag/v1.4.0)에 Windows ZIP/SHA256 첨부.

기존 첫 커밋의 날짜를 과거로 바꾸거나 과거 태그를 소급 생성하지 않았습니다. 이번 문서 PR은 저장소/보호 규칙/릴리스를 다시 구성하지 않습니다.

## 4. 변경된 구성

[.gitignore](../../.gitignore), [.gitattributes](../../.gitattributes), [CI](../../.github/workflows/ci.yml), [공개 검수 도구](../../tools/audit_public.py), [릴리스 빌더](../../tools/build_release.py), README/운영 문서가 근거입니다. 줄바꿈 설정은 기존 Windows 스크립트의 BOM/CRLF 바이트와 업데이트기의 해시 비교를 고려한 구성입니다.

릴리스 빌더는 선택한 커밋의 Git tree만 읽고 새 manifest를 만듭니다. 작업 중 파일/추적되지 않은 운영 데이터를 그대로 복사하는 방식이 아닙니다. GitHub CI에는 카페 노트북 자동 배포 단계가 없습니다.

## 5. 검증 결과

[최종 main CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41029738350): Python 84개 × 4개 OS/버전 조합, 프런트 41개, PowerShell 13개 파서/보조 함수 3개 및 공개 이력 검사 통과. [test_repository.py](../../tests/test_repository.py)는 삭제된 합성 비밀의 Git 이력 검출, 커밋된 바이트 보존, 기존 출력 덮어쓰기 거부 등을 검사합니다.

[공개 검수 기록](../PUBLIC_REVIEW.md)에 실제 값과의 대조/원문 비저장 범위가 남아 있습니다. 검수 도구는 제한된 패턴 검사이며 모든 비밀 형식 검출을 보장하지 않습니다. 보호 규칙 같은 GitHub 설정은 소스 파일만으로 적용됐다고 판단할 수 없으며 [Git 안내](../GIT_GUIDE.md)의 확인 절차와 구분합니다.

## 6. 미검증 사항과 후속 확인

Public 공개/Release 발행은 실제 카페 서비스 배포가 아닙니다. Windows 10 현장 설치·도메인·실물 휴대폰은 별도입니다. 운영 기록/실사용 고객 수/무중단 성과가 새로 생긴 것으로 쓰지 않습니다. 이후 저장소 설정이 바뀌면 현재 상태는 별도로 다시 확인해야 합니다.

## 7. 관련 근거와 학습 확인

[릴리스 반영/코드 롤백](../GITHUB_RELEASES.md) · [기록 원칙](../PORTFOLIO_RECORDS.md) · [TEST_REPORT](../../TEST_REPORT.md).

설명해 볼 질문: 태그/릴리스/운영 배포는 어떻게 다른가? .gitignore로 이미 커밋된 비밀을 제거할 수 있는가? SHA256과 전자서명은 어떤 검증을 구분하는가? 소유자가 직접 설명/재현한 범위는 확인 후 기록합니다.
