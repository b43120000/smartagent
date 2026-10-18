@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    echo [Close Web Pages] ERROR SmartAgent runtime is not installed.
    echo [Close Web Pages] Missing: %SMARTAGENT_PYTHON%
    exit /b 2
)

set "PYTHONPATH=%~dp0source;%PYTHONPATH%"
echo [Close Web Pages] Closing all SmartAgent-owned web pages...
"%SMARTAGENT_PYTHON%" -m agent_core.close_web_pages --root "%~dp0." %*
set "CLOSE_EXIT=%errorlevel%"

if "%CLOSE_EXIT%"=="0" (
    echo [Close Web Pages] Completed.
) else (
    echo [Close Web Pages] Completed with errors. exit_code=%CLOSE_EXIT%
)

endlocal & exit /b %CLOSE_EXIT%
