Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONNOUSERSITE = '1'
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '관리자 권한 PowerShell에서 실행해 주세요.'
    }
}
function Resolve-MaisonRoot([string]$Root) {
    return [IO.Path]::GetFullPath($Root).TrimEnd([char]'\')
}
function Quote-MaisonArg([string]$Value) {
    if ($Value.Contains('"') -or $Value.Contains("`n") -or $Value.Contains("`r") -or $Value.EndsWith('\')) {
        throw '잘못된 실행 인자입니다. 경로 끝의 역슬래시나 따옴표를 제거하세요.'
    }
    return '"' + $Value + '"'
}
function Get-MaisonPython([string]$Root) { return (Join-Path $Root '.venv\Scripts\python.exe') }
function Get-MaisonSettings([string]$Root) {
    return (Get-Content -LiteralPath (Join-Path $Root 'config\operations.json') -Raw -Encoding UTF8 | ConvertFrom-Json)
}
function Get-MaisonTask([string]$Root, [string]$Kind = 'Server') {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant())) }
    finally { $sha.Dispose() }
    $id = ([BitConverter]::ToString($bytes)).Replace('-','').Substring(0,12)
    return 'MaisonCafe-' + $Kind + '-' + $id
}
function Get-OwnedTask([string]$Root, [string]$Kind = 'Server') {
    $task = Get-ScheduledTask -TaskName (Get-MaisonTask $Root $Kind) -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        $expected = if ($Kind -eq 'Server') { 'Run-Server.ps1' } else { 'Backup.ps1' }
        $script = Join-Path $Root ('scripts\' + $expected)
        if (@($task.Actions).Count -ne 1 -or -not $task.Actions[0].Arguments.Contains((Quote-MaisonArg $script))) {
            throw '같은 이름의 다른 작업이 있습니다. 자동으로 변경하지 않습니다.'
        }
    }
    return $task
}
function Set-MaisonAcl([string]$Path) {
    $checkPath=[IO.Path]::GetFullPath($Path)
    while ($checkPath) {
        if ((Test-Path -LiteralPath $checkPath) -and ((Get-Item -LiteralPath $checkPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '정션/연결 경로에는 운영 파일을 설치하지 않습니다.' }
        $parent=Split-Path $checkPath -Parent
        if ($parent -eq $checkPath) { break }
        $checkPath=$parent
    }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity,'FullControl',$inherit,'None','Allow')
        $acl.AddAccessRule($rule)
    }
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Write-MaisonLog([string]$Root, [string]$Message) {
    $directory = Join-Path $Root 'logs'
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $file = Join-Path $directory ('supervisor-' + (Get-Date -Format 'yyyy-MM-dd') + '.log')
    Add-Content -LiteralPath $file -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $Message)
}
function Invoke-MaisonOps {
    param([string]$Root, [string[]]$Arguments, [switch]$Text)
    $python = Get-MaisonPython $Root
    if (-not (Test-Path -LiteralPath $python)) { throw '전용 Python이 없습니다. 설치를 먼저 실행하세요.' }
    $all = @((Join-Path $Root 'scripts\ops.py'),'--root',$Root) + $Arguments
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = (& $python @all 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw ('운영 명령 실패: ' + $output.Trim()) }
    if ($Text) { return $output.Trim() }
    if ($output.Trim()) { return ($output | ConvertFrom-Json) }
}
function Test-MaisonProcess([string]$Root, [int]$IdValue) {
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $IdValue) -ErrorAction SilentlyContinue
    if ($null -eq $process -or -not $process.ExecutablePath -or -not $process.CommandLine) { return $false }
    $python = Get-MaisonPython $Root
    return ($process.ExecutablePath -eq $python -and $process.CommandLine.Contains((Join-Path $Root 'scripts\ops.py')) -and $process.CommandLine.Contains($Root))
}
function Save-MaisonJson([string]$Path, $Value) {
    $temp = $Path + '.new'
    [IO.File]::WriteAllText($temp, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}
function Protect-MaisonTree([string]$Path) {
    # Only for the dedicated install / external MaisonCafe directory, never an entire external disk.
    Set-MaisonAcl $Path
    foreach ($item in Get-ChildItem -LiteralPath $Path -Force -Recurse) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '연결/정션이 포함된 운영 폴더는 지원하지 않습니다.' }
        if ($item.PSIsContainer) { Set-MaisonAcl $item.FullName }
        else {
            $acl=New-Object Security.AccessControl.FileSecurity
            $acl.SetAccessRuleProtection($true,$false)
            foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
                $rule=New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),'FullControl','Allow')
                $acl.AddAccessRule($rule)
            }
            $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
            Set-Acl -LiteralPath $item.FullName -AclObject $acl
        }
    }
}
