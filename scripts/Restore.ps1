param([string]$BackupFile,[string]$Candidate,[switch]$Activate,[string]$Root=(Split-Path $PSScriptRoot -Parent))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
if (-not $Activate) {
    if (-not $BackupFile) { $BackupFile=Read-Host '검증할 백업 파일 전체 경로' }
    Invoke-MaisonOps $Root @('restore-stage','--file',[IO.Path]::GetFullPath($BackupFile)) -Text | Write-Host
    Write-Host '별도 복구 후보 DB만 검증했습니다. 현재 고객 DB는 변경하지 않았습니다.'
    return
}
if (-not $Candidate) { throw '먼저 -BackupFile로 검증한 후보 경로를 -Candidate로 지정하세요.' }
Write-Host '이전 백업 이후의 적립/지급 기록은 현재 상태에서 사라집니다. 실물 지급 내역을 먼저 대조하세요.'
Write-Host ('복구 후보: '+$Candidate)
if ((Read-Host '현재 DB를 보존·분리한 뒤 이 후보를 활성화하려면 RESTORE 입력') -cne 'RESTORE') { return }
& (Join-Path $PSScriptRoot 'Control-Server.ps1') -Root $Root -Action Stop
Invoke-MaisonOps $Root @('restore-activate','--file',[IO.Path]::GetFullPath($Candidate),'--confirm-restore') -Text | Write-Host
Invoke-MaisonOps $Root @('selftest') -Text | Write-Host
Write-Host '복구 활성화 및 별도 DB HTTP 검사 완료. 기록을 확인한 후 관리 메뉴에서 시작하세요.'
