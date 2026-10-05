@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title WebAgent - ChatGPT Direct

set "WEBAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%WEBAGENT_PYTHON%" set "WEBAGENT_PYTHON=python"
if not defined WEBAGENT_SUBMIT_LOCK set "WEBAGENT_SUBMIT_LOCK=%~dp0.agents\webgpt_submit.lock"

if /I "%~1"=="--launcher-self-test" goto launcher_self_test
if /I "%~1"=="--lock-cleanup-self-test" goto lock_cleanup_self_test

call :clear_submit_lock
if errorlevel 1 (
    echo.
    echo [WebAgent] Startup stopped because the previous submit lock could not be removed.
    pause
    exit /b 3
)

echo ============================================================
echo  WebAgent - ChatGPT Direct
echo ============================================================
echo [WebAgent] Paste a ChatGPT conversation URL and press Enter.
echo [WebAgent] The browser will open that tab and send the WebAgent protocol automatically.
echo [WebAgent] Wait for READY, then enter natural-language requests in ChatGPT.
echo.

rem %~dp0 always ends with a backslash. Appending a dot prevents the final
rem backslash from escaping the closing quote in Windows argv parsing.
"%WEBAGENT_PYTHON%" -m WebAgent.controller --workspace "%~dp0."
if errorlevel 1 (
    echo.
    echo [WebAgent] Startup or controller failed with exit code %errorlevel%.
    pause
)
goto done

:clear_submit_lock
echo [WebAgent] Clearing previous ChatGPT submit lock:
echo            %WEBAGENT_SUBMIT_LOCK%
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -LiteralPath '%WEBAGENT_SUBMIT_LOCK%' -Force -ErrorAction SilentlyContinue"
if exist "%WEBAGENT_SUBMIT_LOCK%" (
    echo [WebAgent][ERROR] Submit lock still exists after cleanup.
    exit /b 1
)
echo [WebAgent] Previous ChatGPT submit lock cleared.
exit /b 0

:launcher_self_test
"%WEBAGENT_PYTHON%" -m WebAgent.controller --self-test --workspace "%~dp0." --webgpt-url "https://chatgpt.com/c/webagent-self-test"
if errorlevel 1 exit /b %errorlevel%
echo WEBAGENT_CHATGPT_LAUNCHER_PARSE_OK
goto done

:lock_cleanup_self_test
call :clear_submit_lock
if errorlevel 1 exit /b %errorlevel%
echo WEBAGENT_CHATGPT_LOCK_CLEANUP_OK

:done
endlocal
