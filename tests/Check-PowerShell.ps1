# Portable parser/helper checks; does NOT test Windows cmdlets, ACLs, tasks or services.
$ErrorActionPreference='Stop'
$package=Split-Path $PSScriptRoot -Parent
$count=0
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $package 'scripts') -Filter '*.ps1') {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors)
    if ($errors.Count) { throw ($file.Name+': '+($errors | Out-String)) }
    Write-Output ('Syntax PASS: '+$file.Name)
    $count++
}
. (Join-Path $package 'scripts/Common.ps1')
$value='C:\Maison Cafe\scripts\Run-Server.ps1'
if ((Quote-MaisonArg $value) -cne ('"'+$value+'"')) { throw 'Space-path quoting failed' }
$rejected=$false
try { Quote-MaisonArg 'C:\bad"argument' | Out-Null } catch { $rejected=$true }
if (-not $rejected) { throw 'Unsafe argument accepted' }
if ((Get-MaisonTask 'C:\Maison Cafe') -cne (Get-MaisonTask 'c:\maison cafe')) { throw 'Task name case mismatch' }
Write-Output ('PASS '+$count+' syntax parses + 3 helper checks (no Windows execution).')
