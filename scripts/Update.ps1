param([Parameter(Mandatory=$true)][string]$Package, [string]$Root=(Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
$Package=[IO.Path]::GetFullPath($Package)
Invoke-MaisonOps $Root @('check-package','--file',$Package) | Out-Null
# V1.4 updater only supports app changes with the exact same runtime/operations layer.
foreach ($relative in @('requirements-windows.lock','app\schema.sql') + @(Get-ChildItem -LiteralPath (Join-Path $Root 'scripts') -File | ForEach-Object { 'scripts\'+$_.Name })) {
    $other=Join-Path $Package $relative
    if (-not (Test-Path -LiteralPath $other) -or (Get-FileHash -LiteralPath $other -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Root $relative) -Algorithm SHA256).Hash) { throw '의존성/운영 스크립트가 다른 패키지는 자동 업데이트 대상이 아닙니다. 별도 검토가 필요합니다.' }
}
$wasRunning=(Invoke-MaisonOps $Root @('status')).local_healthy
$stage=Join-Path $Root ('app.next.'+[guid]::NewGuid().ToString('N'))
$old=Join-Path $Root ('backups\code\app-'+(Get-Date -Format 'yyyyMMddTHHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8))
$oldManifest=Get-Content -LiteralPath (Join-Path $Root 'PACKAGE_MANIFEST.json') -Raw -Encoding UTF8
$switched=$false
try {
    $manifest=Get-Content -LiteralPath (Join-Path $Package 'PACKAGE_MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.version -ne '1.4') { throw '이 업데이트기는 V1.4 앱 패치에만 사용합니다. 다른 운영 버전은 별도 이관 검토가 필요합니다.' }
    New-Item -ItemType Directory -Path $stage | Out-Null
    foreach ($entry in $manifest.sha256.PSObject.Properties) {
        if ($entry.Name.StartsWith('app/')) {
            if ($entry.Name.StartsWith('app/data/')) { throw '앱 패키지에 고객 데이터가 포함되어 있습니다.' }
            $destination=Join-Path $stage $entry.Name.Substring(4)
            New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $Package $entry.Name) -Destination $destination
        }
    }
    # Run candidate code against a separate verified SQLite clone, before stopping the live app.
    Invoke-MaisonOps $Root @('--app-dir',$stage,'selftest') | Out-Null
    & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Stop
    & (Join-Path $PSScriptRoot 'Backup.ps1') -Root $Root
    New-Item -ItemType Directory -Path (Split-Path $old -Parent) -Force | Out-Null
    Move-Item -LiteralPath (Join-Path $Root 'app') -Destination $old
    try { Move-Item -LiteralPath $stage -Destination (Join-Path $Root 'app') }
    catch { Move-Item -LiteralPath $old -Destination (Join-Path $Root 'app'); throw }
    $switched=$true
    Invoke-MaisonOps $Root @('selftest') | Out-Null
    # Only app files were changed; retain hashes of the installed docs/operations files.
    $previousManifest=$oldManifest | ConvertFrom-Json
    $hashes=[ordered]@{}
    foreach ($entry in $previousManifest.sha256.PSObject.Properties) {
        if (-not $entry.Name.StartsWith('app/')) { $hashes[$entry.Name]=$entry.Value }
    }
    foreach ($entry in $manifest.sha256.PSObject.Properties) {
        if ($entry.Name.StartsWith('app/')) { $hashes[$entry.Name]=$entry.Value }
    }
    Save-MaisonJson (Join-Path $Root 'PACKAGE_MANIFEST.json') ([ordered]@{format='maison-windows-v1';version='1.4';sha256=$hashes})
    Invoke-MaisonOps $Root @('check-package','--file',$Root) | Out-Null
    if ($wasRunning) { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Start }
    Write-MaisonLog $Root 'App-only update succeeded; config/data preserved.'
    Write-Host ('업데이트 완료. 이전 앱 보관: '+$old)
} catch {
    if ($switched) {
        & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Stop
        $failed=Join-Path $Root ('backups\code\failed-'+[guid]::NewGuid().ToString('N'))
        Move-Item -LiteralPath (Join-Path $Root 'app') -Destination $failed
        Move-Item -LiteralPath $old -Destination (Join-Path $Root 'app')
        [IO.File]::WriteAllText((Join-Path $Root 'PACKAGE_MANIFEST.json'),$oldManifest,(New-Object Text.UTF8Encoding($false)))
    }
    Write-MaisonLog $Root 'Update failed; previous app retained, server left in maintenance if stopped.'
    throw
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
