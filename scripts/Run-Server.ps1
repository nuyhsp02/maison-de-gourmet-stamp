param([string]$Root = (Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
$Root = Resolve-MaisonRoot $Root
$mutex = New-Object Threading.Mutex($false, ('Global\' + (Get-MaisonTask $Root)))
$held = $false
try {
    try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { exit 0 }
    Set-Location -LiteralPath (Join-Path $Root 'app')
    while (-not (Test-Path -LiteralPath (Join-Path $Root 'config\maintenance.flag'))) {
        & (Get-MaisonPython $Root) (Join-Path $Root 'tools\check_sqlite_runtime.py') --require-fixed | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'SQLite WAL 수정 엔진 사전 검사 실패: 서버를 시작하지 않습니다.' }
        Invoke-MaisonOps $Root @('validate') | Out-Null
        Remove-Item -LiteralPath (Join-Path $Root 'config\stop.flag') -Force -ErrorAction SilentlyContinue
        foreach ($name in @('stdout','stderr')) {
            $log = Join-Path $Root ('logs\server-' + $name + '.log')
            if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 5MB) {
                Move-Item -LiteralPath $log -Destination ($log + '.' + (Get-Date -Format 'yyyyMMddHHmmss'))
            }
        }
        $childArguments = @((Join-Path $Root 'scripts\ops.py'),'--root',$Root,'serve')
        $line = ($childArguments | ForEach-Object { Quote-MaisonArg $_ }) -join ' '
        Write-MaisonLog $Root 'Starting production Waitress on loopback.'
        $child = Start-Process -FilePath (Get-MaisonPython $Root) -ArgumentList $line -WorkingDirectory (Join-Path $Root 'app') -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $Root 'logs\server-stdout.log') -RedirectStandardError (Join-Path $Root 'logs\server-stderr.log')
        $child.WaitForExit()
        if (Test-Path -LiteralPath (Join-Path $Root 'config\maintenance.flag')) { break }
        Write-MaisonLog $Root ('Waitress exited: ' + $child.ExitCode + '; retry in 10 seconds.')
        Start-Sleep -Seconds 10
    }
} catch {
    Write-MaisonLog $Root ('Supervisor failure: ' + $_.Exception.Message)
    exit 1
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit 0
