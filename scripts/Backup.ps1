param([string]$Root = (Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
try {
    $config=Get-MaisonSettings $Root
    $arguments=@('backup')
    foreach ($location in $config.ExternalBackupPaths) {
        try {
            if (-not [IO.Path]::IsPathRooted($location)) { throw '외부 백업 경로는 전체 경로여야 합니다.' }
            $dedicated=[IO.Path]::GetFullPath((Join-Path $location 'MaisonCafe'))
            if ($dedicated.StartsWith($Root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw '외부 백업은 설치 폴더 밖에 지정하세요.' }
            New-Item -ItemType Directory -Path $dedicated -Force | Out-Null
            Protect-MaisonTree $dedicated
        } catch {
            Write-MaisonLog $Root 'External backup ACL/path preflight failed; local backup will still run.'
            $arguments+=@('--skip-external',$location)
        }
    }
    Invoke-MaisonOps $Root $arguments -Text | Write-Host
} catch {
    Write-MaisonLog $Root ('Backup failed: '+$_.Exception.Message)
    throw
}
