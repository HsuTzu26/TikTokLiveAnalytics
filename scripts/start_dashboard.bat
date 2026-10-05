@echo off
setlocal EnableExtensions

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_dashboard.ps1" %*
set "DASHBOARD_EXIT_CODE=%ERRORLEVEL%"
if not "%DASHBOARD_EXIT_CODE%"=="0" pause
exit /b %DASHBOARD_EXIT_CODE%
