@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart_system.ps1" %*
set "result=%errorlevel%"
if not "%result%"=="0" echo Restart failed. See the error above.
pause
exit /b %result%
