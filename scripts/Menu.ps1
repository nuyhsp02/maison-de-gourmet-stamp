param([string]$Root=(Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
while ($true) {
    Write-Host "`nMaison de Gourmet 운영 메뉴"
    Write-Host '1 상태  2 시작  3 중지(자동 실행도 일시 중지)  4 재시작  5 최근 오류'
    Write-Host '6 수동 백업  7 백업 복구 검증(현재 DB 유지)  8 자동 실행 등록  9 앱 업데이트  0 종료'
    $choice=Read-Host '번호'
    try {
        switch ($choice) {
            '0' { return }
            '1' { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Status }
            '2' { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Start }
            '3' { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Stop }
            '4' { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Restart }
            '5' { & (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Errors }
            '6' { & (Join-Path $PSScriptRoot 'Backup.ps1') -Root $Root }
            '7' { & (Join-Path $PSScriptRoot 'Restore.ps1') -Root $Root }
            '8' { & (Join-Path $PSScriptRoot 'Register-Autostart.ps1') -Root $Root }
            '9' { $path=Read-Host '새 패키지 압축을 푼 전체 경로'; & (Join-Path $PSScriptRoot 'Update.ps1') -Root $Root -Package $path }
            default { Write-Host '번호를 다시 선택하세요.' }
        }
    } catch { Write-Host ('처리 실패: '+$_.Exception.Message) -ForegroundColor Red }
}
