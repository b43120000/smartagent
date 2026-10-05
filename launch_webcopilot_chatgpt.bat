@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "SMARTAGENT_SOURCE=%~dp0source"
if not exist "%SMARTAGENT_SOURCE%\agent_core\__init__.py" (
    echo [SmartAgent] ERROR Canonical source is missing: %SMARTAGENT_SOURCE%
    exit /b 2
)
set "PYTHONPATH=%SMARTAGENT_SOURCE%;%PYTHONPATH%"
set "PYTHONSAFEPATH=1"
set "SMARTAGENT_SECURITY_PROFILE=%~dp0localdata\secure\windows_security\security_profile.json"
title WebAgent - ChatGPT Direct

set "WEBAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%WEBAGENT_PYTHON%" (
    echo [WebAgent] ERROR This SmartAgent folder is not installed yet.
    echo [WebAgent] Missing: %WEBAGENT_PYTHON%
    echo [WebAgent] Run install_smart_agent.bat in this folder first.
    exit /b 2
)
if /I "%~1"=="--launcher-self-test" goto launcher_self_test
if not defined WEBAGENT_SUBMIT_LOCK for /f "delims=" %%P in ('""%WEBAGENT_PYTHON%" -m agent_core.path_cli webgpt_submit_lock --root "%~dp0.""') do set "WEBAGENT_SUBMIT_LOCK=%%P"
if not defined WEBAGENT_SUBMIT_LOCK (
    echo [WebAgent] ERROR Unable to resolve ChatGPT submit lock path.
    exit /b 2
)
set "SMARTAGENT_BOOTSTRAP_MODE=0"
if /I "%~1"=="--bootstrap-provision" set "SMARTAGENT_BOOTSTRAP_MODE=1"

if /I "%~1"=="--lock-cleanup-self-test" goto lock_cleanup_self_test
if /I "%~1"=="--enable-test-ingress" set "SMARTAGENT_TEST_INGRESS=1"
"%WEBAGENT_PYTHON%" -m agent_core.agent_switch --root "%~dp0." --keep webdirect

rem Complete any interactive Local/WebCopilot first-run setup before starting
rem the runtime monitor. The monitor intentionally abandons PREPARED startups
rem after 15 seconds, so user input must never happen inside that window.
if not "%SMARTAGENT_BOOTSTRAP_MODE%"=="1" (
    "%WEBAGENT_PYTHON%" -m WebAgent.controller --prepare-startup-only %*
    if errorlevel 1 (
        echo [WebAgent] Local/WebCopilot startup preparation failed.
        exit /b 2
    )
)

call :prepare_runtime webdirect "Tri-One WebDirect Monitor"
if errorlevel 1 exit /b 3

echo ============================================================
echo  WebAgent - ChatGPT Direct
echo ============================================================
if "%SMARTAGENT_BOOTSTRAP_MODE%"=="1" (
    echo [WebAgent] First-run provisioning mode.
    echo [WebAgent] The application root is the temporary provisioning workspace.
    echo [WebAgent] Complete ChatGPT login manually if requested.
) else (
    echo [WebAgent] Using workspace and ChatGPT conversation saved by Edit_workspace.bat.
    echo [WebAgent] Explicit --workspace and --webgpt-url arguments override saved values.
)
echo [WebAgent] Wait for READY before interacting with the conversation.
echo.

if "%SMARTAGENT_BOOTSTRAP_MODE%"=="1" (
    if defined SMARTAGENT_BOOTSTRAP_WEBGPT_URL (
        "%WEBAGENT_PYTHON%" -m WebAgent.controller --workspace "%~dp0." --bootstrap-provision --webgpt-url "%SMARTAGENT_BOOTSTRAP_WEBGPT_URL%"
    ) else (
        "%WEBAGENT_PYTHON%" -m WebAgent.controller --workspace "%~dp0." --bootstrap-provision
    )
) else (
    "%WEBAGENT_PYTHON%" -m WebAgent.controller --use-saved-startup %*
)
if errorlevel 1 (
    echo.
    echo [WebAgent] Startup or controller failed with exit code %errorlevel%.
    pause
)
goto done

:prepare_runtime
set "SMARTAGENT_RUNTIME_INTERFACE=%~1"
set "SMARTAGENT_RUNTIME_MONITOR_REQUIRED=1"
set "SMARTAGENT_RUNTIME_GENERATION="
for /f "usebackq delims=" %%G in (`"%WEBAGENT_PYTHON%" -m agent_core.runtime_cleanup prepare --interface %~1`) do set "SMARTAGENT_RUNTIME_GENERATION=%%G"
if not defined SMARTAGENT_RUNTIME_GENERATION (
    echo [WebAgent] PRESTART_CLEANUP failed.
    exit /b 1
)
echo [WebAgent] PRESTART_CLEANUP complete. generation=%SMARTAGENT_RUNTIME_GENERATION%
start "%~2" "%WEBAGENT_PYTHON%" -m agent_core.runtime_monitor --interface %~1 --generation "%SMARTAGENT_RUNTIME_GENERATION%"
exit /b 0

:launcher_self_test
"%WEBAGENT_PYTHON%" -m WebAgent.controller --self-test --workspace "%~dp0." --webgpt-url "https://chatgpt.com/c/webagent-self-test"
if errorlevel 1 exit /b %errorlevel%
echo WEBAGENT_CHATGPT_LAUNCHER_PARSE_OK
goto done

:lock_cleanup_self_test
"%WEBAGENT_PYTHON%" -m agent_core.runtime_cleanup recover-lock --path "%WEBAGENT_SUBMIT_LOCK%"
if errorlevel 1 exit /b %errorlevel%
echo WEBAGENT_CHATGPT_LOCK_CLEANUP_OK

:done
endlocal
