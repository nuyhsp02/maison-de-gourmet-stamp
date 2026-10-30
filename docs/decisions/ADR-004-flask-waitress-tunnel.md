# ADR-004 · Flask + Waitress + Cloudflare Tunnel 구조

- 상태: 앱/설정 스크립트 확인 / 사후 기록. 최초 결정일: **미확인**.
- 기준: [V1.4.0](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f), [현재 구조](../../README.md#시스템-구조).

## 1. 해결하려던 문제

Windows 노트북의 앱에 고객이 서로 다른 네트워크에서 고정 HTTPS 주소로 접근해야 합니다. 앱의 보안 쿠키·카메라 흐름을 유지하면서 노트북의 Python 포트를 외부에 직접 공개하지 않는 운영 구성이 필요합니다.

## 2. 고려한 대안과 기록의 한계

기존 Flask/Waitress 유지와 Tunnel 사용 조건은 확인됩니다. 개발용 서버, 직접 포트 공개, 별도 원격 호스팅을 실제 비교 시험한 기록은 없습니다. 이들은 사후 비교 후보입니다. 기존 Caddy 예제는 로컬 HTTPS 테스트 자료로 남아 있으며, 그 사실만으로 운영 프록시 후보 평가를 했다고 해석하지 않습니다.

## 3. 최종 선택

Flask가 API/정적 화면을 제공하고 Waitress는 `127.0.0.1`에서 실행됩니다. 운영 공개 경로는 고정 도메인의 HTTPS → Cloudflare Tunnel → 같은 노트북의 loopback HTTP로 준비합니다. 원본의 Host/Origin 검사와 loopback 프록시 신뢰 범위를 유지합니다. 실제 도메인/토큰은 별도 설정합니다.

## 4. 기술적 선택 근거

현재 앱 팩토리·Waitress 실행 옵션을 재사용하고 운영 설정/서버 시작을 분리할 수 있습니다. 고객 공개 HTTPS와 노트북 내부 HTTP를 구별합니다. 이는 현재 요구와 구현의 적합성 설명이며, Tunnel의 실접속 성공이나 다른 호스팅보다 우수하다는 측정 결과는 아닙니다.

## 5. 단점과 위험

전원·인터넷·DNS/인증서·터널 서비스에 의존합니다. 로컬 health 또는 터널 ready만으로 고객의 전체 HTTPS 경로가 검증되지는 않습니다. Host/프록시 설정 오류도 요청 실패 원인이 됩니다. 토큰 파일은 제한된 ACL로 보관하지만 관리자·디스크 접근 위험까지 없애지는 않습니다.

## 6. 실제 코드 및 테스트 근거

- [ops.py](../../scripts/ops.py): `create_http`, `request_health`, `selftest`, `serve`, loopback 및 신뢰 프록시 옵션.
- [app.py](../../app/app.py): 운영 HTTPS/draft 검사, Host/Origin/CSRF, 보안 헤더.
- [Setup-Tunnel.ps1](../../scripts/Setup-Tunnel.ps1): token-file 인자·서비스 등록 구성. **실제 등록 수행 증거가 아님**.
- [test_windows_ops.py](../../tests/test_windows_ops.py): `test_real_waitress_selftest_uses_clone_and_closes_listener`, `test_real_server_restart_duplicate_process_and_persisted_cookie` — 임시 loopback 시험.
- [Cloudflare 운영 안내](../CLOUDFLARE.md), [Windows 인수표](../WINDOWS_ACCEPTANCE.md): 실제 공개/서비스 검증은 별도.

## 7. 향후 개선 가능성

도메인 확정 후 DNS/TLS/Host 전달·LTE/5G·단절 후 재접속을 점검합니다. 운영 중단을 허용할 수 없다면 호스팅 구조를 새 ADR에서 비교할 수 있습니다. 이번 기록은 배포 설정을 바꾸거나 실제 공개를 수행하지 않습니다.
