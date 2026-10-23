@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set "build_exit=%errorlevel%"
pause
exit /b %build_exit%
