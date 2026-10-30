param([string]$Root = 'C:\MaisonCafe', [string]$PythonExe, [string]$EventFile, [string]$PublicUrl, [switch]$PrepareOnly)
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Admin
$Root=Resolve-MaisonRoot $Root
$package=Split-Path $PSScriptRoot -Parent
if ($PSVersionTable.PSVersion -lt [version]'5.1' -or -not [Environment]::Is64BitProcess) { throw '64비트 Windows PowerShell 5.1 이상이 필요합니다.' }
$os=Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -lt 19045 -or -not [Environment]::Is64BitOperatingSystem) { throw 'Windows 10 22H2 (19045) 이상, 64비트가 필요합니다.' }
if ($Root.StartsWith('\\') -or $Root -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\') -or $Root -eq $package) { throw '설치 경로는 패키지 폴더와 다른 로컬 전용 폴더를 지정하세요.' }
Quote-MaisonArg $Root | Out-Null
$drive=Get-Volume -DriveLetter ([IO.Path]::GetPathRoot($Root).Substring(0,1))
if ($drive.FileSystem -ne 'NTFS') { throw '설치 드라이브는 NTFS여야 합니다. USB FAT/exFAT에는 설치하지 마세요.' }
if (-not $PythonExe) {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($version in @('-3.13','-3.12')) {
            $previous=$ErrorActionPreference
            try {
                $ErrorActionPreference='Continue'
                $probe=& py.exe $version -c 'import sys; print(sys.executable)' 2>$null
                $probeExit=$LASTEXITCODE
            } finally { $ErrorActionPreference=$previous }
            if ($probeExit -eq 0) { $PythonExe=($probe | Select-Object -Last 1); break }
        }
    }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) { throw '공식 64비트 CPython 3.13을 모든 사용자용으로 설치하세요. 필요하면 -PythonExe 전체경로를 지정하세요.' }
$PythonExe=[IO.Path]::GetFullPath($PythonExe)
if ($PythonExe -match '(?i)\\Users\\|WindowsApps|Anaconda|Miniconda') { throw 'SYSTEM 자동 실행을 위해 사용자 전용/Store/Conda Python 대신 모든 사용자용 CPython을 사용하세요.' }
$probe=& $PythonExe -c 'import sys,struct,json,sysconfig; print(json.dumps([sys.implementation.name,sys.version_info.major,sys.version_info.minor,struct.calcsize("P")*8,bool(sysconfig.get_config_var("Py_GIL_DISABLED"))]))'
if ($LASTEXITCODE -ne 0) { throw 'Python 실행 실패' }
$info=$probe | ConvertFrom-Json
if ($info[0] -ne 'cpython' -or $info[1] -ne 3 -or $info[2] -notin @(12,13) -or $info[3] -ne 64 -or $info[4]) { throw '지원 범위는 일반 GIL 빌드 CPython 3.12/3.13 x64입니다.' }
$check=& $PythonExe (Join-Path $package 'scripts\ops.py') --root $package check-package --file $package
if ($LASTEXITCODE -ne 0) { throw '패키지 무결성 검증 실패' }
$marker=Join-Path $Root 'config\installation.json'
$existing=Test-Path -LiteralPath $marker
if ((Test-Path -LiteralPath $Root) -and -not $existing -and @(Get-ChildItem -LiteralPath $Root -Force).Count -gt 0) { throw '기존의 다른 폴더에는 설치하지 않습니다. 빈 전용 폴더를 선택하세요.' }
if ($existing) {
    $installed=Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($installed.Version -ne '1.4') { throw '다른 버전입니다. 업데이트 절차를 사용하세요.' }
    & $PythonExe (Join-Path $Root 'scripts\ops.py') --root $Root check-package --file $Root
    if ($LASTEXITCODE -ne 0) { throw '설치 파일이 변경되었습니다. 재설치로 덮어쓰지 않습니다.' }
} else {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    Set-MaisonAcl $Root
    foreach ($name in @('app','config','data','backups','logs','scripts','.venv')) { New-Item -ItemType Directory -Path (Join-Path $Root $name) -Force | Out-Null }
    $manifest=Get-Content -LiteralPath (Join-Path $package 'PACKAGE_MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $manifest.sha256.PSObject.Properties) {
        $destination=Join-Path $Root $entry.Name
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $package $entry.Name) -Destination $destination
    }
    Copy-Item -LiteralPath (Join-Path $package 'PACKAGE_MANIFEST.json') -Destination $Root
    Save-MaisonJson $marker ([ordered]@{Version='1.4';Installed=(Get-Date -Format o);Root=$Root})
}
# New files inherit the restricted root ACL. Never copy existing config/data from the package.
Set-MaisonAcl $Root
$configPath=Join-Path $Root 'config\operations.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    $config=Get-Content -LiteralPath (Join-Path $Root 'config\operations.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $PublicUrl -and -not $PrepareOnly) { $PublicUrl=Read-Host '정식 HTTPS 주소 (미정이면 Enter: 준비만)' }
    if ($PublicUrl) { $config.PublicUrl=$PublicUrl.TrimEnd('/') }
    Save-MaisonJson $configPath $config
} elseif ($PublicUrl) { Write-Host '기존 operations.json을 보존했습니다. 주소 변경은 파일을 직접 확인하세요.' }
$eventPath=Join-Path $Root 'config\event.json'
if (-not (Test-Path -LiteralPath $eventPath)) {
    if (-not $EventFile -and -not $PrepareOnly) { $EventFile=Read-Host '확정된 event.json 전체 경로 (미정이면 Enter: 준비만)' }
    if ($EventFile) { Copy-Item -LiteralPath $EventFile -Destination $eventPath }
} elseif ($EventFile) { Write-Host '기존 config\event.json을 보존했습니다.' }
$python=Get-MaisonPython $Root
if (-not (Test-Path -LiteralPath $python)) {
    & $PythonExe -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { throw '가상환경 생성 실패' }
}
# On rerun, refuse a mismatched venv; do not silently change a live runtime.
$venvVersion=& $python -c 'import sys; print(str(sys.version_info.major)+"."+str(sys.version_info.minor))'
if ($LASTEXITCODE -ne 0 -or $venvVersion -ne ('3.'+$info[2])) { throw '기존 가상환경 Python 버전이 다릅니다. 운영 중인 환경을 자동 교체하지 않습니다.' }
if ($existing -and (Test-Path -LiteralPath (Join-Path $Root 'data\collection.sqlite'))) {
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw '기존 의존성이 손상되었습니다. 서버 중지 후 복구 절차를 확인하세요.' }
} else {
    & $python -m pip install --disable-pip-version-check --only-binary=:all: -r (Join-Path $Root 'requirements-windows.lock')
    if ($LASTEXITCODE -ne 0) { throw '의존성 설치 실패: 인터넷 연결 및 Python 버전을 확인하세요. DB/설정은 보존됩니다.' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw '의존성 충돌' }
}
Invoke-MaisonOps $Root @('check-runtime') | Out-Null
Protect-MaisonTree $Root
$config=Get-MaisonSettings $Root
if ($PrepareOnly -or -not $config.PublicUrl -or -not (Test-Path -LiteralPath $eventPath)) {
    Write-Host ('준비 설치 완료: '+$Root)
    Write-Host '도메인·확정 event.json 설정 전에는 DB 생성/서버 실행/외부 공개를 하지 않습니다. 설정 후 설치를 다시 실행하세요.'
    return
}
Invoke-MaisonOps $Root @('initialize') -Text | Write-Host
Invoke-MaisonOps $Root @('selftest') -Text | Write-Host
Write-Host '설정/DB/로컬 Waitress 검증 완료. 기존 DB는 초기화하지 않았습니다.'
if ((Read-Host '부팅 자동 실행과 일일 백업 등록 및 서버 시작? (Y 입력)') -match '^[Yy]$') {
    & (Join-Path $Root 'scripts\Register-Autostart.ps1') -Root $Root -StartNow
}
Write-Host 'Cloudflare 연결은 별도 설정입니다. Windows 설치 설명서의 실기기 검증을 진행하세요.'
