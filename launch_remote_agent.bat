@echo off
setlocal
if /I "%~1"=="--launcher-self-test" echo REMOTE_AGENT_LAUNCHER_PARSE_OK
if /I "%~1"=="--launcher-self-test" exit /b 0
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
title SmartAgent - RemoteAgent-0 Receiver
set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    echo.
    echo [RemoteAgent-0] ERROR SmartAgent runtime is not installed for this launcher.
    echo [RemoteAgent-0] Missing: %~dp0.venv\Scripts\python.exe
    echo [RemoteAgent-0] Run install_smart_agent.bat first, then start RemoteAgent again.
    pause
    exit /b 2
)

set "SMARTAGENT_LAUNCH_LOG="
for /f "delims=" %%P in ('""%SMARTAGENT_PYTHON%" -m agent_core.path_cli remote_restart_log --root "%~dp0.""') do set "SMARTAGENT_LAUNCH_LOG=%%P"
if not defined SMARTAGENT_LAUNCH_LOG (
    echo [RemoteAgent] ERROR Unable to resolve remote restart log path.
    exit /b 2
)
for %%D in ("%SMARTAGENT_LAUNCH_LOG%") do if not exist "%%~dpD" mkdir "%%~dpD" >nul 2>&1
>>"%SMARTAGENT_LAUNCH_LOG%" echo [%date% %time%] LAUNCH_BAT_START %*
if exist "%SMARTAGENT_SECURITY_PROFILE%" set "SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED=1"
set "SMARTAGENT_FORCE_STOP_FLAG="
for /f "delims=" %%P in ('""%SMARTAGENT_PYTHON%" -m agent_core.path_cli force_stop_flag --root "%~dp0.""') do set "SMARTAGENT_FORCE_STOP_FLAG=%%P"
if not defined SMARTAGENT_FORCE_STOP_FLAG (
    echo [RemoteAgent] ERROR Unable to resolve force-stop flag path.
    exit /b 2
)
if exist "%SMARTAGENT_FORCE_STOP_FLAG%" del /q "%SMARTAGENT_FORCE_STOP_FLAG%" >nul 2>&1

if /I "%~1"=="telegram-pair" goto telegram_pair
"%SMARTAGENT_PYTHON%" -m agent_core.agent_switch --root "%~dp0." --keep remote
goto start

:telegram_pair
if "%SMARTAGENT_TELEGRAM_BOT_TOKEN%"=="" (
    echo [Telegram pairing] SMARTAGENT_TELEGRAM_BOT_TOKEN is required.
    echo Set it only in this terminal, then run: launch_remote_agent.bat telegram-pair
    goto done
)
set "SMARTAGENT_TELEGRAM_ENABLED=1"
set "SMARTAGENT_TELEGRAM_PAIRING_ENABLED=1"
if "%SMARTAGENT_TELEGRAM_WORKSPACE%"=="" set "SMARTAGENT_TELEGRAM_WORKSPACE=%~dp0"
"%SMARTAGENT_PYTHON%" -m RemoteAgent.telegram_pairing
if errorlevel 1 goto done
echo [Telegram pairing] Pairing completed. Starting the Agent0 receiver.

:start
if exist "%SMARTAGENT_FORCE_STOP_FLAG%" goto force_stop
"%SMARTAGENT_PYTHON%" -m agent_core.security_preflight --interface remote
set "SECURITY_PREFLIGHT_EXIT=%errorlevel%"
>>"%SMARTAGENT_LAUNCH_LOG%" echo [%date% %time%] PREFLIGHT_EXIT code=%SECURITY_PREFLIGHT_EXIT%
if not "%SECURITY_PREFLIGHT_EXIT%"=="0" (
    echo [RemoteAgent-0] SECURITY_PREFLIGHT failed. Listener will remain available; mutation workers are disabled.
)
call :prepare_runtime remote "Tri-One RemoteAgent Monitor"
if errorlevel 1 (
    echo [RemoteAgent-0] ERROR Receiver bootstrap failed. Automatic restart is disabled.
    pause
    goto done
)
echo [RemoteAgent-0] Starting the remote signal receiver.
>>"%SMARTAGENT_LAUNCH_LOG%" echo [%date% %time%] RUNTIME_PREPARED generation=%SMARTAGENT_RUNTIME_GENERATION%
echo [RemoteAgent-0] WAITING_SIGNAL - RemoteAgent owns its remote lifecycle and will not start LocalAgent.
"%SMARTAGENT_PYTHON%" -m agent_core.host_supervisor --remote-only
set "REMOTE_SUPERVISOR_EXIT=%errorlevel%"
>>"%SMARTAGENT_LAUNCH_LOG%" echo [%date% %time%] SUPERVISOR_EXIT code=%REMOTE_SUPERVISOR_EXIT%
"%SMARTAGENT_PYTHON%" -m agent_core.runtime_cleanup finalize-exit --interface remote --generation "%SMARTAGENT_RUNTIME_GENERATION%" --reason "remote_supervisor_exit_%REMOTE_SUPERVISOR_EXIT%"
if exist "%SMARTAGENT_FORCE_STOP_FLAG%" goto force_stop
echo [RemoteAgent-0] Receiver process exited (code=%REMOTE_SUPERVISOR_EXIT%).
if %REMOTE_SUPERVISOR_EXIT%==0 goto done
echo [RemoteAgent-0] ERROR Startup/runtime failed. Automatic restart is disabled to prevent repeated popup windows.
echo [RemoteAgent-0] Fix the reported configuration error, then start RemoteAgent again.
pause
goto done

:force_stop
echo [RemoteAgent-0] FORCE_STOP_ALL_AGENTS received. Receiver will not restart.
del /q "%SMARTAGENT_FORCE_STOP_FLAG%" >nul 2>&1
goto done

:failed
echo.
echo [RemoteAgent-0] Startup failed with exit code %errorlevel%.
echo [RemoteAgent-0] Automatic restart is disabled.
pause
goto done

:prepare_runtime
set "SMARTAGENT_RUNTIME_INTERFACE=%~1"
set "SMARTAGENT_RUNTIME_MONITOR_REQUIRED=1"
set "SMARTAGENT_RUNTIME_GENERATION="
set "SMARTAGENT_PREPARE_OUTPUT=%TEMP%\smartagent_prepare_%RANDOM%_%RANDOM%.txt"
"%SMARTAGENT_PYTHON%" -m agent_core.runtime_cleanup prepare --interface %~1 >"%SMARTAGENT_PREPARE_OUTPUT%" 2>&1
set "SMARTAGENT_PREPARE_EXIT=%errorlevel%"
type "%SMARTAGENT_PREPARE_OUTPUT%" >>"%SMARTAGENT_LAUNCH_LOG%"
set /p "SMARTAGENT_RUNTIME_GENERATION="<"%SMARTAGENT_PREPARE_OUTPUT%"
del /q "%SMARTAGENT_PREPARE_OUTPUT%" >nul 2>&1
if not "%SMARTAGENT_PREPARE_EXIT%"=="0" (
    >>"%SMARTAGENT_LAUNCH_LOG%" echo [%date% %time%] PREPARE_FAILED exit_code=%SMARTAGENT_PREPARE_EXIT%
    echo [RemoteAgent-0] PRESTART_CLEANUP failed.
    exit /b 1
)
if not defined SMARTAGENT_RUNTIME_GENERATION (
    >>"%SMARTAGENT_LAUNCH_LOG%" echo [%date% %time%] PREPARE_FAILED exit_code=%SMARTAGENT_PREPARE_EXIT%
    echo [RemoteAgent-0] PRESTART_CLEANUP failed.
    exit /b 1
)
echo [RemoteAgent-0] PRESTART_CLEANUP complete. generation=%SMARTAGENT_RUNTIME_GENERATION%
start "" /b "%SMARTAGENT_PYTHON%" -m agent_core.runtime_monitor --interface %~1 --generation "%SMARTAGENT_RUNTIME_GENERATION%"
exit /b 0

:done
endlocal
