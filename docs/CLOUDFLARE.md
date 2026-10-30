# Cloudflare Tunnel — 고정 도메인과 Windows 서비스

이 문서는 **이후 운영 담당자가 실제 공개할 때** 사용하는 절차입니다. 패키지 제작 과정에서 도메인/터널을 만들거나 연결하지 않았습니다. 도메인과 계정/토큰은 운영자가 준비하며 Quick Tunnel 임시 주소를 고객 QR에 사용하지 않습니다.

## 연결 구조

```text
고객 Safari/Chrome (Wi-Fi/LTE/5G)
    → https://고정도메인 (공인 HTTPS)
    → Cloudflare Tunnel
    → Windows Cloudflared 서비스 (외부로 연결)
    → http://127.0.0.1:8765 (Waitress)
    → C:\MaisonCafe\data\collection.sqlite
```

공유기 포트 포워딩/인바운드 방화벽 개방은 하지 않습니다. 로컬 API/메트릭 포트도 외부 공개하지 않습니다. 127.0.0.1에서 HTTP를 쓰는 것은 같은 노트북 내부 연결이며 고객 웹은 HTTPS입니다. 공인 HTTPS가 있어야 Secure 쿠키·카메라 권한·복구 링크를 실제 모바일 환경에서 사용할 수 있습니다. `http://노트북LAN주소`는 이 운영 설정의 고객 테스트 주소가 아닙니다.

## 1. 계정과 고정 주소

1. 운영자가 소유·관리하는 도메인을 Cloudflare에서 사용할 수 있도록 준비합니다. 해당 계정 권한/서비스 정책/비용은 직접 확인합니다.
2. Cloudflare 대시보드의 Tunnel에서 remotely-managed 터널을 만듭니다.
3. 공개 hostname을 `config\operations.json`의 PublicUrl 호스트와 동일하게 설정하고 service는 `http://127.0.0.1:8765`로 지정합니다(Port를 바꾸었다면 같은 값).
4. Host 헤더를 `localhost`나 `127.0.0.1`로 바꾸지 않습니다. 기본 전달을 사용하거나 원본 HTTP Host Header를 **공개 도메인**으로 명시합니다. 기존 앱이 Host를 검사합니다. HTTPS 요청의 전달 스킴 헤더도 유지합니다.
5. 고객 공개 경로에 Access 로그인 등 인증 장벽을 추가하지 않습니다. HTML/API/QR/복구에 강제 캐시를 설정하지 않고 앱의 `Cache-Control: no-store`를 존중합니다. 도메인의 HTTP 접속은 HTTPS로 리디렉션하도록 확인합니다.
6. 고객 방문 전에 TLS 인증서 발급 상태와 DNS 반영을 확인합니다. 로컬 Caddy·휴대폰 사설 루트 인증서 설치는 이번 운영 경로에 필요하지 않습니다. 기존 `app/Caddyfile.example`은 과거 안내용 파일입니다.

대시보드 메뉴 이름은 서비스 업데이트로 달라질 수 있습니다. [공식 터널 토큰 안내](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)를 기준으로 해당 터널의 토큰만 준비합니다. 계정 API 토큰/명령문 전체를 입력하지 않습니다.

## 2. 실행 파일과 서비스 등록

[Cloudflare 공식 배포](https://github.com/cloudflare/cloudflared/releases)에서 Windows amd64 실행 파일을 내려받고 배포 출처/체크섬을 확인합니다. 최소 `2025.4.0`(token-file 지원) 이상이며 **현재 보안 패치 버전**을 선택합니다. EXE는 패키지에 포함하지 않았고 자동 다운로드/업데이트하지 않습니다. 운영 중 업데이트는 서비스 중지 후 담당자가 검토해 진행합니다.

먼저 카페 앱 설치·검증을 완료하고 다음을 관리자 PowerShell에서 실행합니다.

```powershell
& "C:\MaisonCafe\scripts\Setup-Tunnel.ps1" -CloudflaredExe "C:\설치자료\cloudflared-windows-amd64.exe"
```

스크립트는 버전·`tunnel run --help`의 token-file 지원을 확인하고 EXE를 `bin\cloudflared.exe`로 복사합니다. 기존 `Cloudflared` 서비스가 있으면 덮어쓰지 않습니다. 같은 노트북의 다른 터널 운영자가 있다면 먼저 조율합니다. 서비스 이름은 cloudflared의 Windows 구현에 맞춰 `Cloudflared`를 사용합니다.

토큰은 `Read-Host -AsSecureString`으로 가려 입력받아 `config\tunnel-token.txt`에 저장합니다. 설치 루트는 관리자·SYSTEM 전용 NTFS ACL이며 새 토큰 파일도 이 제한을 상속합니다. 토큰 파일은 서비스 사용을 위해 평문으로 저장되지만 소스/서비스 명령행/스크립트 출력에 토큰 값을 넣지 않습니다. 관리자 권한자나 디스크 접근 권한자는 읽을 수 있으므로 노트북 계정/디스크 보호도 필요합니다. 화면 녹화·전체 PowerShell transcript·토큰 파일 출력은 하지 않습니다. 토큰 값으로 명령을 직접 실행하지 마세요.

서비스 명령의 형태(값 대신 **파일 경로**):

```text
"C:\MaisonCafe\bin\cloudflared.exe" tunnel --no-autoupdate --loglevel error --logfile "C:\MaisonCafe\logs\tunnel.log" --metrics 127.0.0.1:20241 run --token-file "C:\MaisonCafe\config\tunnel-token.txt"
```

기본 LocalSystem/자동 시작 서비스로 등록합니다. SCM 실패 복구는 첫 5초/다음 15초/이후 60초 재시작, 실패 횟수 하루 리셋으로 설정합니다. 네트워크 단절 시 cloudflared 자체 재연결과 서비스 종료 시 SCM 복구를 사용합니다. 실제 재연결·SCM 연동은 Windows 실기기 검증 대상이며 이 스크립트 제작만으로 동작 보증하지 않습니다. 서비스의 작업 디렉터리에 기대지 않고 모든 경로를 절대 경로로 전달합니다.

[공식 실행 옵션](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/), [Windows 서비스 구현](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/windows_service.go)을 확인해 작성했습니다. 서비스 설치 명령의 인자에 토큰을 넣는 방식은 사용하지 않습니다. error 수준 로그로 시작 설정 전체 출력도 피합니다.

## 3. 상태 확인 / 장애 분리

`Manage.cmd` → `1 상태`에서 서버/터널/백업을 확인합니다. 또는:

```powershell
Get-Service Cloudflared
& "C:\MaisonCafe\scripts\Control-Server.ps1" -Action Status
Get-Content -LiteralPath "C:\MaisonCafe\logs\tunnel.log" -Tail 40
```

로그는 고객에게 공유하지 말고 담당자에게 필요한 부분만 전달합니다. 절대로 `Get-Content config\tunnel-token.txt`를 진단 명령으로 쓰지 않습니다.

- **서버 정상 / 터널 미연결**: 인터넷/Wi-Fi(로그인 전 연결 포함), Windows 시간, 서비스 상태, 토큰 유효성, 아웃바운드 보안 정책을 확인합니다. [Cloudflare 연결 요건](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)을 따릅니다. 포트 포워딩을 추가해 해결하지 않습니다.
- **터널 연결됨 / 고객 화면 502**: Waitress의 실제 Port, 원본 `127.0.0.1` 주소, 앱 상태 확인. 서버가 중지/점검 모드인지 확인합니다.
- **고객 화면 400 invalid_host**: 공개 주소와 operations.json/Cloudflare HTTP Host Header가 같은지 확인합니다. 앱 Host 검사를 끄지 않습니다.
- **403/쿠키 반복 분실**: 도메인·HTTPS·Origin·브라우저 쿠키 정책 확인. 인앱 브라우저는 기본 Safari/Chrome과 저장 공간이 다를 수 있어 같은 브라우저로 다시 열거나 기존 개인 복구 링크를 사용합니다.
- **연결 상태는 정상인데 QR/카메라 실패**: 아래 모바일 점검표를 따릅니다. 자동 인식/카메라 권한은 HTTPS와 브라우저마다 다르며 Windows 서버 상태만으로 검증할 수 없습니다.

내부 `/ready`는 cloudflared 메트릭 서버의 연결 상태입니다. 응답 200은 커넥터 준비 상태이고 DNS/TLS/실제 앱의 전체 성공을 의미하지 않습니다. [공식 metrics 구현](https://github.com/cloudflare/cloudflared/blob/master/metrics/metrics.go)을 확인했습니다. **휴대폰 Wi-Fi를 끄고 실제 고정 HTTPS 주소와 상품 QR을 열어 보는 검사**가 필요합니다.

서비스 재시작:

```powershell
Restart-Service Cloudflared
```

서버를 재시작해도 터널을 매번 재설치할 필요는 없습니다. 장기간 장애 시 해당 로그·예약 작업 결과·인터넷 상태를 기록하고 담당자에게 전달합니다.

## 4. 토큰 교체 / 서비스 해제

점검 시간에 Cloudflare에서 기존 토큰을 회전/폐기하고 서비스 중지 후 교체합니다. 소스/로그에 새 토큰을 붙이지 않습니다.

1. `Remove-Tunnel.ps1`을 실행해 **이 설치에서 등록한 서비스**만 해제합니다(경로/서비스 명령 일치 검사).
2. 보존된 `config\tunnel-token.txt`는 담당자가 토큰 폐기 상태를 확인한 뒤 삭제합니다. 재사용하지 않습니다.
3. 새 공식 EXE로 `Setup-Tunnel.ps1`을 실행하고 새 토큰을 보안 입력합니다. 외부 HTTPS·재부팅 연결을 다시 확인합니다.

```powershell
& "C:\MaisonCafe\scripts\Remove-Tunnel.ps1"
```

서비스/토큰 제거는 앱 DB·고객 기록·QR 키를 지우지 않습니다. 대시보드 DNS/터널 자체 삭제는 별도 담당자 결정입니다.
