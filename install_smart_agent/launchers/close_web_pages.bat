@echo off
for %%R in ("%~dp0..\..") do set "SMARTAGENT_PACKAGE_ROOT=%%~fR\"
setlocal
chcp 65001 >nul 2>&1
cd /d "%SMARTAGENT_PACKAGE_ROOT%"
call "%SMARTAGENT_PACKAGE_ROOT%install_smart_agent\launchers\ensure_portable_runtime.bat"
if errorlevel 1 exit /b %ERRORLEVEL%

set "SMARTAGENT_PYTHON=%SMARTAGENT_PACKAGE_ROOT%.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    echo [Close Web Pages] ERROR SmartAgent runtime is not installed.
    echo [Close Web Pages] Missing: %SMARTAGENT_PYTHON%
    exit /b 2
)

set "PYTHONPATH=%SMARTAGENT_PACKAGE_ROOT%source;%PYTHONPATH%"
echo [Close Web Pages] Closing all SmartAgent-owned web pages...
"%SMARTAGENT_PYTHON%" -m agent_core.close_web_pages --root "%SMARTAGENT_PACKAGE_ROOT%." %*
set "CLOSE_EXIT=%errorlevel%"

if "%CLOSE_EXIT%"=="0" (
    echo [Close Web Pages] Completed.
) else (
    echo [Close Web Pages] Completed with errors. exit_code=%CLOSE_EXIT%
)

endlocal & exit /b %CLOSE_EXIT%
