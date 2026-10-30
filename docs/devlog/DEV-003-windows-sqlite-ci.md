# DEV-003 · Windows CI에서 발견한 SQLite 연결·쓰기 경합

- 기록 유형: 실패 실행·수정 커밋·재검사로 대조 가능한 회고.
- 발생/수정일: **2026-10-30**, 아래 Git/Actions 기록으로 확인.
- 실행 주체: GitHub Windows/Ubuntu hosted runner, AI를 활용한 수정·로컬 검수. 소유자의 개인 Windows 장비 직접 재현 여부는 미확인.

## 1. 문제와 예상 동작

Windows 검사에서 DB를 지우거나 복구 후보로 교체할 때 열린 파일 오류가 발생했고, 릴리스 테스트의 줄바꿈 기대도 달랐습니다. 이후 동시 쓰기 검사에서는 503이 남았습니다. 기대 동작은 사용을 끝낸 DB 연결의 해제, 커밋된 바이트의 보존, 기존 동시 적립/지급 검사 통과입니다.

## 2. 관측과 원인

[첫 실패 CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41027960458)에서 Windows 파일 접근 실패와 LF/CRLF 기대값 불일치가 확인됩니다. 초기 `with connection`은 트랜잭션 종료와 연결 close를 같은 것으로 취급한 구현이었고, 읽기 fixture에도 명시적 close가 빠져 있었습니다. 연결 해제는 별도 회귀 검사로 고정했습니다.

[후속 실패 CI](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41028565269)에서는 Python 3.12 Windows의 합성 동시 클라이언트 검사에서 `storage_unavailable`/503이 확인됩니다. 쓰기 경합에 대응해 프로세스 내부 쓰기 요청을 조정했습니다. 당시 캡처만으로 모든 실패의 SQLite 세부 오류 코드, OS 스케줄링/드라이버별 원인까지 확정할 수는 없습니다.

## 3. 해결 과정

1. 초기화 성공/이벤트 불일치 예외 경로 모두 연결이 닫히도록 수정했습니다.
2. 테스트의 읽기 연결을 닫고, ZIP 바이트를 OS 고정 줄바꿈이 아닌 실제 커밋 내용과 비교했습니다.
3. 남은 쓰기 경합에 단일 앱 프로세스의 쓰기 슬롯을 적용했습니다. GET은 이 슬롯을 점유하지 않으며 요청 종료 시 DB 연결 정리 후 슬롯을 해제합니다.
4. 기존 부하/중복 검사를 생략하지 않고 다시 실행했습니다.

이 과정은 AI 지원 구현과 자동 실행 기록입니다. 개인이 직접 모든 원인을 재현/분석한 경험으로 바꾸어 서술하지 않습니다.

## 4. 코드 변경

- [7c6e749 · 연결 해제/fixture 수정](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/7c6e7491c94d97e807e5952b7f0a38c7d7e9f9a1).
- [acb2f61 · 쓰기 요청 조정](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/acb2f612af48cc2e0a0ef16c3c2be0930dd83416).
- [app.py](../../app/app.py): `init_db`의 closing, `app.write_lock`, before_request/teardown의 획득·해제, SQL 오류 분류 기록.
- [test_windows_ops.py](../../tests/test_windows_ops.py): 초기화 연결 해제, GET 독립성, 실패 후 쓰기 슬롯 해제 검사. [test_repository.py](../../tests/test_repository.py): 커밋된 바이트 비교.

API·스키마·중복 방지 제약·운영 PowerShell 스크립트는 유지했습니다. 이번 기록 작업은 이 코드를 다시 수정하지 않습니다.

## 5. 검증 결과

[최종 main CI / 1284c52](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41029738350)에서 Ubuntu/Windows × Python 3.12/3.13 각각 84개가 통과했습니다. 프런트 41개와 PowerShell 파서/보조 함수도 통과했습니다. [로컬 요약](../evidence/github-python-local.txt)은 별도 환경의 84개 통과 기록입니다.

`test_400_visitors_30_concurrent`는 임시 SQLite와 Flask 애플리케이션 클라이언트의 합성 부하입니다. 실사용 고객 400명이나 실제 외부 HTTP 처리량을 달성했다는 결과가 아닙니다.

## 6. 미검증 사항과 후속 확인

실제 Windows 10 노트북의 설치·부팅·장시간 대기/실패율은 미확인입니다. 메모리 쓰기 슬롯은 다중 프로세스/서버의 전역 잠금이 아니고 공정한 FIFO나 처리 속도를 보장하지 않습니다. [현장 인수표](../WINDOWS_ACCEPTANCE.md)를 따로 수행해야 합니다.

## 7. 관련 근거와 학습 확인

[PR #1](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/1) · [ADR-001](../decisions/ADR-001-sqlite-persistence.md) · [ADR-005](../decisions/ADR-005-windows-and-backup.md).

설명해 볼 질문: 트랜잭션 종료와 close는 어떻게 다른가? 쓰기 슬롯과 DB 고유 제약은 각각 어떤 문제를 막는가? 소유자의 직접 설명/재현 기록은 실제 수행 뒤 별도로 남깁니다.
