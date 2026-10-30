# DEV-008 · 공식 Python 3.15.2 안전 런타임 호환성

기록일: 2026-10-31 (Asia/Seoul). 기존 [PR #4](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/4)에서 한정된 런타임 호환성 작업입니다. [DEV-007](DEV-007-transaction-write-scope.md)의 B 앱/스키마/15초/내구성은 바꾸지 않았습니다. PR #3와 main, v1.4.0, 실제 고객 DB/설정은 유지합니다.

## 실제 공식 배포본 확인

공식 Python 문서가 안내하는 [CPython NuGet `python` 3.15.2](https://www.nuget.org/packages/python/3.15.2)를 공식 NuGet v3 source에서 버전 고정으로 받았습니다. free-threaded 별도 패키지를 쓰지 않았습니다. 각 runner의 기존 Python을 교체하지 않고 전체 배포본/venv를 격리했습니다. 패키지 SHA256은 두 다운로드 모두 `294F5ED7AB97148FCBB57B8C06BA428F6D282C2BBAA266DBE3137284497998F0`입니다. 이 기록을 별도 서명 검증 주장으로 바꾸지 않습니다.

- sys.version: `3.15.2 (tags/v3.15.2:5e58eaa, Oct 25 2026, 17:27:03) [MSC v.1951 64 bit (AMD64)]`
- implementation cpython / bits 64 / free_threaded false / gil_enabled true
- SQLite: **3.53.4**
- sqlite_source_id(): `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`

실제 ID가 [공식 SQLite 3.53.4 릴리스](https://sqlite.org/releaselog/3_53_4.html)의 ID와 일치합니다. [WAL-reset 수정](https://sqlite.org/wal.html#walreset)을 포함하는 버전입니다. [Python 3.15 Windows 지원](https://docs.python.org/3.15/using/windows.html#supported-windows-versions)은 Windows 10 이상이며, 본 설치기는 기존 Windows 10 22H2 x64/build 19045 조건을 유지합니다. Windows CI는 실제 카페 Windows 10 장비가 아니므로 현장 확인은 별도입니다.

## 초기 변경 없는 lock 호환성

[실행 41078188035 / attempt 1](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41078188035/attempts/1): Windows 두 독립 반복 모두 성공했습니다. 실제 수정 엔진 확인 뒤 기존 requirements-test.lock(운영 lock 포함)을 **--only-binary=:all:**로 설치했고 pip check 및 모든 운영 의존성 import가 성공했습니다. lock 버전 변경은 없습니다.

각 반복 전체 Python 98개(실제 Waitress selftest/시작·정지·재시작, 백업·복원 포함)와 JavaScript 41개가 통과했습니다. 원본 400명·동시 30개와 동일 함수 계측 검사가 고객 400/스탬프 4000/지급 400, 중복/무결성 조건을 유지했습니다. 계측별 5200회 소유 완료입니다. [실제 엔진·source ID·패키지 해시·통계 원본](../evidence/python315-initial-validation.json).

## 설치 변경과 실제로 발견한 실패

통과 후에만 Install.ps1을 수정했습니다. 3.15 탐색을 우선하되 자동 설치를 억제하고 일반 GIL/x64 및 검증한 3.15.2 패치를 확인합니다. 기존 3.12/3.13은 코드 호환성 대상이나 수정 SQLite 사전 확인이 필요합니다. 현재 관측된 구 엔진은 설치 폴더 변경 전에 거절합니다. 선택한 base와 기존 venv의 실제 빌드/전체 버전을 각각 검사하고 자동 교체하지 않습니다. 의존성 wheel-only 정책은 유지합니다. Run-Server.ps1도 Waitress 실행 전에 실제 venv의 수정 엔진을 검사합니다.

[설치 검증 실패 41079111501](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41079111501/attempts/1) 두 작업은 Python/의존성/회귀 검사는 통과했지만 설치에서 실패했습니다. PS5.1의 native 인자 처리로 인라인 Python 문자열 따옴표가 사라져 `struct.calcsize(P)`의 NameError가 발생했습니다. 단순 재시도로 숨기지 않고, 검사 파일을 직접 실행해 JSON을 읽도록 바꿨습니다. venv 검사도 같은 방식입니다.

[수정 후 41080033522](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41080033522/attempts/1)는 두 작업 모두 성공했습니다. Windows PowerShell 5.1에서 실제 Install.ps1 -PrepareOnly, 가상환경 생성, wheel 설치와 검사, 임시 DB/상품 QR/고객 생성 후 반복 설치를 수행해 DB·설정·설치 표식의 바이트 보존을 확인했습니다. 정식 ZIP/tag/Release가 아니라 CI 임시 staging manifest만 만들었습니다. 실제 카페 경로는 사용하지 않았습니다.

추가로 반복 설치 전후 venv 설정·실행 파일 해시도 비교하고, 3.15.2를 기존 필수 CI의 Ubuntu/Windows matrix에 추가합니다. 3.12/3.13 호환성 검사는 유지합니다. 최종 결과와 검토 상태는 완료 후 아래에 기록합니다.

## 운영 범위

[새 설치 안내](../WINDOWS_INSTALL.md)는 공유 경로의 전체 공식 배포본을 명시하고 기존 venv 자동 마이그레이션을 금지합니다. [백엔드 학습 자료](../BACKEND_LEARNING.md)와 기존 B 논리는 유지합니다. 검증된 후보의 코드 리뷰와 실제 운영 승인/정식 릴리스/배포는 구분합니다. SYSTEM 부팅 작업, 실제 노트북 ACL·전원/절전·Windows 업데이트, 도메인/Tunnel HTTPS, 실물 휴대폰은 현장 인수표에서 확인해야 합니다.


필수 matrix 확장 직후 [41080943401](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41080943401/attempts/1)은 YAML plain scalar의 `:all:` 인용 누락으로 job 생성 전 실패했습니다. 로컬 YAML parser에서도 44행 오류를 재현하고 명령 전체를 인용해 수정했습니다. 앱/의존성 검사 실패나 성공으로 집계하지 않습니다.


필수 CI 확장에서는 [Ubuntu 3.15.2 / 41081422115](https://github.com/nuyhsp02/maison-de-gourmet-stamp/actions/runs/41081422115/attempts/1)의 실제 엔진이 시스템 SQLite 3.45.1(`2024-01-30 ...alt1`)로 수정 범위 검사에 실패했습니다. Windows NuGet/Windows setup-python의 배포와 다르며 Python 버전만으로 엔진을 판정하지 않아야 한다는 근거입니다. Ubuntu vendor 백포트는 검증하지 않았습니다. Linux 런타임 교체를 이번 작업에 추가하지 않습니다.

엄격한 운영 엔진 검사는 이 프로젝트의 운영 대상인 Windows 3.15.2에서 필수로 유지합니다. Ubuntu 3.12/3.13/3.15.2는 실제 엔진을 출력하고 원본 전체 테스트를 모두 실행하는 호환성 CI로 구분합니다. Ubuntu의 데이터/부하 검사 강도나 성공 조건을 줄이지 않으며, 그 CI 성공을 Ubuntu 운영 엔진 안전성 승인으로 취급하지 않습니다.
