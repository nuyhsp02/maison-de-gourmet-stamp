param([string]$Root = (Split-Path $PSScriptRoot -Parent), [switch]$StartNow)
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root = Resolve-MaisonRoot $Root
Invoke-MaisonOps $Root @('validate') | Out-Null
$config=Get-MaisonSettings $Root
$time=[DateTime]::ParseExact($config.DailyBackupTime,'HH:mm',[Globalization.CultureInfo]::InvariantCulture)
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$ps=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
foreach ($kind in @('Server','Backup')) {
    Get-OwnedTask $Root $kind | Out-Null
    $script=if ($kind -eq 'Server') {'Run-Server.ps1'} else {'Backup.ps1'}
    $arguments='-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File '+(Quote-MaisonArg (Join-Path $Root ('scripts\'+$script)))+' -Root '+(Quote-MaisonArg $Root)
    $action=New-ScheduledTaskAction -Execute $ps -Argument $arguments -WorkingDirectory $Root
    if ($kind -eq 'Server') {
        $trigger=New-ScheduledTaskTrigger -AtStartup
        $settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    } else {
        $trigger=New-ScheduledTaskTrigger -Daily -At $time
        $settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    }
    Register-ScheduledTask -TaskName (Get-MaisonTask $Root $kind) -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description ('MaisonCafe V1.4 '+$kind+' '+$Root) -Force | Out-Null
}
Write-Host '부팅 자동 실행 및 매일 백업 작업 등록 완료. 암호를 저장하지 않습니다.'
if ($StartNow) { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Start }
