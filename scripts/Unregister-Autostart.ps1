param([string]$Root = (Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
& (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Stop
foreach ($kind in @('Server','Backup')) {
    $task=Get-OwnedTask $Root $kind
    if ($null -ne $task) { Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false }
}
Write-Host '자동 실행 작업만 해제했습니다. DB/설정/백업은 보존됩니다.'
