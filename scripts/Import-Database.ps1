param([Parameter(Mandatory=$true)][string]$BackupFile,[string]$Root=(Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
Invoke-MaisonOps $Root @('import-database','--file',[IO.Path]::GetFullPath($BackupFile)) -Text | Write-Host
Write-Host '기존 SQLite 백업에서 가져왔습니다. 원본 백업은 보존했습니다. 대상 DB가 이미 있으면 이 작업은 거부됩니다.'
