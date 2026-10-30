param([ValidateSet('Status','Start','Stop','Restart','Errors')][string]$Action = 'Status', [string]$Root = (Split-Path $PSScriptRoot -Parent), [switch]$ForceStop)
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root = Resolve-MaisonRoot $Root
function Stop-OwnServer {
    $task = Get-OwnedTask $Root
    if ($null -ne $task) { Disable-ScheduledTask -TaskName $task.TaskName | Out-Null }
    New-Item -ItemType File -Path (Join-Path $Root 'config\maintenance.flag') -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $Root 'config\stop.flag') -Force | Out-Null
    $statePath = Join-Path $Root 'config\server.state.json'
    $limit = (Get-Date).AddSeconds(40)
    while ((Test-Path -LiteralPath $statePath) -and (Get-Date) -lt $limit) {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not (Test-MaisonProcess $Root $state.pid)) { break }
        Start-Sleep -Seconds 1
    }
    if (Test-Path -LiteralPath $statePath) {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (Test-MaisonProcess $Root $state.pid) {
            if (-not $ForceStop) { throw '서버 종료가 지연됩니다. 상태/로그 확인 후 필요한 경우 -ForceStop으로 다시 실행하세요.' }
            Stop-Process -Id $state.pid -Force
        }
    }
    if ($null -ne $task) { Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue }
    Write-MaisonLog $Root 'Server stopped; automatic startup paused until Start.'
}
function Start-OwnServer {
    Invoke-MaisonOps $Root @('validate') | Out-Null
    $task = Get-OwnedTask $Root
    if ($null -eq $task) { throw '자동 실행 작업을 먼저 등록하세요.' }
    Remove-Item -LiteralPath (Join-Path $Root 'config\maintenance.flag') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $Root 'config\stop.flag') -Force -ErrorAction SilentlyContinue
    Enable-ScheduledTask -TaskName $task.TaskName | Out-Null
    Start-ScheduledTask -TaskName $task.TaskName
    for ($n=0; $n -lt 30; $n++) {
        Start-Sleep -Seconds 1
        $status = Invoke-MaisonOps $Root @('status')
        if ($status.local_healthy) { Write-Host '서버 정상 실행'; return }
    }
    throw '시작 확인 시간 초과. 오류 로그를 확인하세요.'
}
switch ($Action) {
    'Start' { Start-OwnServer }
    'Stop' { Stop-OwnServer; Write-Host '서버 중지 완료' }
    'Restart' { Stop-OwnServer; Start-OwnServer }
    'Errors' {
        foreach ($file in @('errors.log','server-stderr.log','tunnel.log')) {
            $path=Join-Path $Root ('logs\'+$file)
            if (Test-Path -LiteralPath $path) { Write-Host ('--- '+$file); Get-Content -LiteralPath $path -Tail 40 -Encoding UTF8 }
        }
    }
    'Status' {
        $status=Invoke-MaisonOps $Root @('status')
        Write-Host ('카페 서버: '+$(if ($status.local_healthy) {'정상'} else {'응답 없음'}))
        Write-Host ('터널 연결: '+$(if ($status.tunnel_ready) {'연결됨 (외부 HTTPS 접속도 확인 필요)'} else {'미연결 / 미설정'}))
        Write-Host ('점검 모드: '+$status.maintenance)
        Write-Host ('디스크 여유: '+[Math]::Round($status.free_bytes/1GB,2)+' GB')
        if ($status.backup) { Write-Host ('최근 백업: '+$status.backup.time+' / '+$status.backup.status); Write-Host ('파일: '+$status.backup.daily) }
        else { Write-Host '성공한 백업 기록 없음' }
        if ($status.last_backup_failure) { Write-Host ('마지막 백업 실패 기록: '+$status.last_backup_failure.time+' (최근 성공 시각과 대조)') }
        if ($status.tunnel_log_bytes -gt 50MB) { Write-Host '터널 로그 50MB 초과. 설치 설명서의 로그 정리 절차를 확인하세요.' }
        foreach ($kind in @('Server','Backup')) {
            $task=Get-OwnedTask $Root $kind
            if ($null -ne $task) {
                $info=Get-ScheduledTaskInfo -TaskName $task.TaskName
                Write-Host ($kind+' 작업: '+$task.State+' / 마지막 결과 '+$info.LastTaskResult+' / 다음 실행 '+$info.NextRunTime)
            } else { Write-Host ($kind+' 작업 미등록') }
        }
    }
}
