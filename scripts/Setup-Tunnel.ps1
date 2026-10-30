param([string]$Root=(Split-Path $PSScriptRoot -Parent), [Parameter(Mandatory=$true)][string]$CloudflaredExe)
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
Invoke-MaisonOps $Root @('validate') | Out-Null
if (Get-Service -Name Cloudflared -ErrorAction SilentlyContinue) { throw 'Cloudflared 서비스가 이미 있습니다. 덮어쓰지 않습니다. 기존 운영자를 확인하세요.' }
$CloudflaredExe=[IO.Path]::GetFullPath($CloudflaredExe)
$version=(& $CloudflaredExe --version | Out-String)
if ($LASTEXITCODE -ne 0 -or $version -notmatch '(\d{4})\.(\d+)\.(\d+)') { throw '공식 cloudflared 실행 파일/버전을 확인하세요.' }
if ([version]$Matches[0] -lt [version]'2025.4.0') { throw 'token-file 지원 cloudflared 2025.4.0 이상이 필요합니다. 최신 보안 패치 버전을 사용하세요.' }
$help=(& $CloudflaredExe tunnel run --help | Out-String)
if ($LASTEXITCODE -ne 0 -or $help -notmatch '\-\-token-file') { throw '이 실행 파일에서 token-file 옵션을 확인하지 못했습니다.' }
$binary=Join-Path $Root 'bin\cloudflared.exe'
New-Item -ItemType Directory -Path (Split-Path $binary -Parent) -Force | Out-Null
Copy-Item -LiteralPath $CloudflaredExe -Destination $binary -Force
$tokenPath=Join-Path $Root 'config\tunnel-token.txt'
if (Test-Path -LiteralPath $tokenPath) { throw '기존 토큰 파일을 보존합니다. 설치 설명서의 토큰 교체 절차를 확인하세요.' }
Write-Host 'Cloudflare 대시보드에서 고정 도메인 → HTTP 127.0.0.1:8765(설정한 Port) 경로를 먼저 확인하세요.'
Write-Host '명령문 전체가 아니라 토큰 값만 입력하세요. 화면/로그/서비스 명령행에 토큰을 표시하지 않습니다.'
$secure=Read-Host '터널 토큰' -AsSecureString
$pointer=[IntPtr]::Zero
try {
    $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if (-not $plain -or $plain -notmatch '^[A-Za-z0-9_\-+/=]+$') { throw '토큰 형식을 확인하세요.' }
    [IO.File]::WriteAllText($tokenPath,$plain,(New-Object Text.UTF8Encoding($false)))
} finally {
    $plain=$null
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    $secure.Dispose()
}
$config=Get-MaisonSettings $Root
# Error-level logging avoids startup settings dumps containing sensitive configuration.
$command=(Quote-MaisonArg $binary)+' tunnel --no-autoupdate --loglevel error --logfile '+(Quote-MaisonArg (Join-Path $Root 'logs\tunnel.log'))+' --metrics 127.0.0.1:'+$config.MetricsPort+' run --token-file '+(Quote-MaisonArg $tokenPath)
New-Service -Name Cloudflared -BinaryPathName $command -DisplayName 'MaisonCafe Cloudflare Tunnel' -StartupType Automatic -Description ('MaisonCafe '+$Root) | Out-Null
Save-MaisonJson (Join-Path $Root 'config\tunnel-service.json') ([ordered]@{Root=$Root;Binary=$binary;Command=$command;Version=$version.Trim()})
& sc.exe failure Cloudflared reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
if ($LASTEXITCODE -ne 0) { throw '서비스 재시도 설정 실패. 상태를 확인하세요.' }
& sc.exe failureflag Cloudflared 1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw '서비스 종료 복구 설정 실패' }
Start-Service -Name Cloudflared
Write-Host '서비스 시작 요청 완료. 관리 메뉴의 상태와 외부 HTTPS 접속을 확인하세요. 서비스 Running만으로 외부 연결 성공을 뜻하지 않습니다.'
