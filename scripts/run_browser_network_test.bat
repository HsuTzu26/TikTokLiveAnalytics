@echo off
setlocal EnableExtensions

set "STREAMER_HANDLE=%~1"
if not defined STREAMER_HANDLE set /p "STREAMER_HANDLE=TikTok LIVE username (without @): "
if not defined STREAMER_HANDLE (
    echo Username is required.
    exit /b 2
)

set "RUNNER=%~dp0run_browser_network_test.ps1"
if not exist "%RUNNER%" (
    echo Missing runner: "%RUNNER%"
    exit /b 2
)

set "RAW_FRAME_OPTION="
if /I "%~3"=="raw" set "RAW_FRAME_OPTION=-SaveRawFrames"
set "KEEP_CACHE_OPTION="
if /I "%~4"=="keepcache" set "KEEP_CACHE_OPTION=-KeepBrowserCache"

if "%~2"=="" (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%" -StreamerUsername "%STREAMER_HANDLE%" %RAW_FRAME_OPTION% %KEEP_CACHE_OPTION%
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%" -StreamerUsername "%STREAMER_HANDLE%" -DurationMinutes "%~2" %RAW_FRAME_OPTION% %KEEP_CACHE_OPTION%
)
exit /b %ERRORLEVEL%
