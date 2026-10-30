@echo off
setlocal
set "MAISON_PACKAGE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "$a='-NoProfile -ExecutionPolicy RemoteSigned -NoExit -File '+[char]34+$env:MAISON_PACKAGE+'scripts\Menu.ps1'+[char]34; Start-Process powershell.exe -Verb RunAs -ArgumentList $a -Wait"
if errorlevel 1 pause
endlocal
