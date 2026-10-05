@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "ENGINE=%~dp0install_smart_agent\install_milestones.ps1"
if not exist "%ENGINE%" (
    echo [ERROR] Milestone engine not found: %ENGINE%
    pause
    exit /b 2
)
set "HAD_ARGS="
set "MILESTONE="
set "WANT_JSON="
set "WANT_QUIET="
:parse
if "%~1"=="" goto run
set "HAD_ARGS=1"
if /I "%~1"=="--json" set "WANT_JSON=1"& shift& goto parse
if /I "%~1"=="--quiet" set "WANT_QUIET=1"& shift& goto parse
if /I "%~1"=="--milestone" (
    if "%~2"=="" echo [ERROR] --milestone requires M0-M5.& exit /b 2
    set "MILESTONE=%~2"
    shift
    shift
    goto parse
)
echo [ERROR] Unknown argument: %~1
exit /b 2
:run
set "FIXED_ARGS="
if defined WANT_JSON set "FIXED_ARGS=%FIXED_ARGS% -Json"
if defined WANT_QUIET set "FIXED_ARGS=%FIXED_ARGS% -Quiet"
if defined MILESTONE (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ENGINE%" -Action Check -ProjectRoot "%~dp0." -Milestone "%MILESTONE%" %FIXED_ARGS%
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ENGINE%" -Action Check -ProjectRoot "%~dp0." %FIXED_ARGS%
)
set "RESULT=%ERRORLEVEL%"
if not defined HAD_ARGS pause
exit /b %RESULT%
