# Windows 10 로컬 Waitress 수동 실행 — 2026-11-01

**판정: 사용자 전달 PowerShell 관측값 기준으로 전경 기동·loopback health·Ctrl+C 뒤 종료 및 빈 합성 DB의 0/0/0 유지 확인. 전체 현장 인수·운영 승인 아님. PR #4 Draft 유지.**

## 근거와 시험 구분

- 수행자: 사용자. 실제 Windows 10 노트북에서 직접 조작하고 결과를 전달함. 일자 **2026-11-01 (KST)**.
- AI는 해당 노트북에 원격 접속하거나 이번 서버를 실행하지 않았다. **Windows 원본 로그·상태 파일·DB를 확보하거나 직접 검토하지 않았다.** 아래 표는 사용자 전달 관측값의 Markdown 기록이며 원본 파일을 재구성한 증거가 아니다.
- 대상 후보는 기존 `7667f5c5745dc712e23587213229dd5e0e6713ff`이다. 패키지/런타임 기준과 이전 확인은 [앞선 HTTP 합성 시험 기록](windows10-http-2026-11-01-7667f5c.md)에 연결한다. 이번 전달에 별도 해시 재계산·런타임 재검사 결과는 없으므로 새로 확인했다고 쓰지 않는다.
- 이전 **47고객/237스탬프/1지급**은 `C:\MaisonFieldRuns\field-7667-01`의 합성 도구 결과이다. 이번은 **`C:\MaisonCafe-Acceptance`의 별도 시험 DB**에서 수동 실행한 결과다. 47/237/1을 0/0/0으로 초기화했다는 기록이 아니며 기존 합성 시험 증거를 수정하거나 대체하지 않는다.
- Windows Server CI·macOS tooling-smoke·실제 Windows 10 사용자 수행을 구분한다. 기존 [인수 계획](../WINDOWS_ACCEPTANCE.md)과 [수동 실행 계획 M01~M03](../FIELD_SETUP.md#다음-단계-격리-로컬-waitress-수동-실행-계획)에 연결한다.

## 실제 사용자 전달 관측값

| 순서 / 항목 | 전달받은 결과 |
|---|---|
| 시험 Root | `C:\MaisonCafe-Acceptance` |
| 시험 origin | `https://field.example.invalid` |
| TEST 설정 | 생성 및 적용 완료 |
| `ops.py initialize` | **ready=true, customers=0, stamps=0, reward_audit=0** |
| 초기화 후 `ops.py validate` | **정상** |
| `ops.py serve` | 실제 Windows 10에서 실행 |
| 실행 중 `ops.py status` | **local_healthy=true, maintenance=false, tunnel_ready=false** |
| 서버 PID | **5384** |
| 시작 기록 | **2026-11-01T16:08:52.411645+09:00** |
| 로컬 리스너 | **127.0.0.1:8875, PID 5384 일치** |
| 종료 조작 | **Ctrl+C 뒤 PowerShell 프롬프트 복귀** |
| 종료 후 `ops.py status` | **local_healthy=false, server=null** |
| 종료 후 리스너 | **8875 포트 리스너 없음** |
| 최종 `ops.py validate` | **ready=true, 고객 0명, 스탬프 0개, 지급 기록 0건** |

PID는 이 실행에서 관측된 값이며 이후 실행을 위한 고정 PID나 종료 대상으로 사용하지 않는다. 시작 기록은 사용자 전달값이며 노트북 시계 정확도를 별도 검증했다는 뜻이 아니다. 정확한 종료 시각·기동/종료 소요 시간·각 명령의 개별 종료 코드는 미제공이므로 추정하지 않는다.

## 확인한 범위와 남은 범위

| 구분 | 이번 기록의 판정 |
|---|---|
| TEST 설정·새 빈 합성 DB 준비 | initialize/validate 정상과 0/0/0을 사용자 전달로 확인 |
| 전경 서버 기동 | serve 실행, local_healthy=true, PID와 loopback 리스너 일치 확인 |
| 수동 종료 | Ctrl+C 프롬프트 복귀, local_healthy=false/server=null, 리스너 없음 확인 |
| 종료 뒤 DB | validate 정상과 **빈 DB의 0/0/0 유지** 확인. 기록이 있는 고객/쿠키/QR/복구/지급의 재시작 보존 검사를 새로 수행한 것은 아님 |
| 원본·상태 파일 | 미확보. server=null 관측을 기록하며 상태 파일의 직접 존재 조회나 파일 바이트 비교를 했다고 쓰지 않음 |
| M01 사전 검사의 모든 항목 | 이번 전달에 없는 개별 패키지/권한/종료 코드 검사를 소급 통과 처리하지 않음 |
| H07 / 기존 Windows 중지·재시작 항목 | **전경 기동/종료 부분만 확인**. 재실행·메뉴 중지/시작·재부팅·감독 복구·로그인 전 자동 실행은 미실행 |
| HTTPS·브라우저·카메라 | **미실행**. 설정 origin 문자열은 실제 DNS/TLS 연결 증거가 아님 |
| Cloudflare / 자동 실행 / 장시간 운영 / 실제 카페 운영 | **미실행** |

`ops.py status`는 소스상 설정된 Host로 loopback `/health`를 조회한다. 이 해석은 기존 코드의 동작에 근거하며, 실제 HTTPS나 고객 적립 POST를 이번에 수행했다는 근거가 아니다. `tunnel_ready=false`는 Tunnel을 검증하지 않은 단계의 관측값으로 보존한다. 최종 validate 정상은 전달 결과이며 별도의 SQLite 원시 검사 출력이나 DB 파일을 독립 확보했다고 표현하지 않는다.

## 기존 기록과 운영 경계

- [앞선 HTTP 도구 기록](windows10-http-2026-11-01-7667f5c.md)의 9개 사례·400건 집계·47/237/1과 의도적 503/응답 유실은 그대로 보존한다. 원본 `field-tool-report.json` 독립 검토도 여전히 미완료다. 이번 수동 실행을 그 도구의 재시험으로 세지 않는다.
- 전체 현장 인수 상태는 **not_performed**로 유지한다. 기존 지연 기준은 운영자 미승인 제안이며 이번 결과에 별도 성능 측정은 없다.
- 보안 업데이트/ESU는 **보류·미해결**, 과거 간헐 admission timeout의 근본 원인은 **미확정**이다. 빈 DB의 기동/종료 성공으로 두 문제를 해결했다고 판단하지 않는다.
- 직원 인증 없는 지급과 DB 지급 1회/실물 전달 1회의 차이는 남은 운영 위험이다. 이번에는 고객 적립이나 실제 보상을 시험하지 않았다.
- [PR #4](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/4)는 Draft·미병합 유지. 운영 코드/API/스키마/잠금 경계/15초/의존성 변경, main 병합, Release/태그 생성, 실제 카페 배포는 하지 않는다.

## 다음 단계 — 계획만, 미실행

[HTTPS·실물 휴대폰 시험 계획](../FIELD_SETUP.md#후속-계획-격리-https와-실물-휴대폰-시험)으로 이어진다. 시험 도메인·접속 범위·OS 보안 조건·별도 Root/DB/호스트·변경 승인을 먼저 확정한다. **현재 보안/ESU 보류를 외부 공개 승인으로 바꾸지 않는다.** 기존 시험 Root의 설정·DB를 HTTPS용으로 덮어쓰지 않고, 기존 합성 결과 폴더도 보존한다. 이 문서 작성 과정에서 HTTPS/Tunnel/휴대폰 시험이나 추가 부하 검사를 실행하지 않았다.
