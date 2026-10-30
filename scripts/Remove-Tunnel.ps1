param([string]$Root=(Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
$marker=Join-Path $Root 'config\tunnel-service.json'
if (-not (Test-Path -LiteralPath $marker)) { throw '이 설치에서 등록한 서비스인지 확인할 수 없습니다.' }
$owner=Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
$service=Get-CimInstance Win32_Service -Filter "Name='Cloudflared'"
if ($null -eq $service -or $service.PathName -ne $owner.Command -or $owner.Root -ne $Root) { throw '서비스 소유 정보 불일치. 다른 서비스를 변경하지 않습니다.' }
Stop-Service -Name Cloudflared
& sc.exe delete Cloudflared | Out-Null
if ($LASTEXITCODE -ne 0) { throw '서비스 해제 실패' }
Remove-Item -LiteralPath $marker
Write-Host '터널 서비스 해제 완료. 토큰 파일은 보존했습니다. Cloudflare에서 토큰 폐기 후 파일을 안전하게 삭제하세요.'
