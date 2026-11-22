@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /I "%~1"=="--launcher-self-test" (
    echo GIT62_TRANSACTION_UPDATE_LAUNCHER_PARSE_OK
    exit /b 0
)
if "%~1"=="" (
    echo Usage: update.bat FULL_40_CHARACTER_COMMIT_SHA [--verify]
    exit /b 2
)
set "TARGET_SHA=%~1"
set "VERIFY_OPT="
if /I "%~2"=="--verify" set "VERIFY_OPT=-VerifyOnly"
if not "%~2"=="" if /I not "%~2"=="--verify" (
    echo [SmartAgent Update] Unknown option. Only --verify is supported.
    exit /b 2
)
if not "%~3"=="" (
    echo [SmartAgent Update] Unsupported arguments.
    exit /b 2
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_smart_agent\git62_transaction_update.ps1" -Commit "%TARGET_SHA%" -ProjectRoot "%~dp0." %VERIFY_OPT%
set "RESULT=%ERRORLEVEL%"
endlocal & exit /b %RESULT%
