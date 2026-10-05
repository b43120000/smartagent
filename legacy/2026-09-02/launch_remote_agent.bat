@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - RemoteAgent-0 Receiver
set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    set "SMARTAGENT_PYTHON=python"
    echo [RemoteAgent-0] Project .venv was not found; using Python from PATH.
    echo [RemoteAgent-0] Run install_smart_agent.bat first if dependencies are missing.
)

if /I "%~1"=="telegram-pair" goto telegram_pair
if /I "%~1"=="--launcher-self-test" goto launcher_self_test
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
if errorlevel 1 goto failed
echo [Telegram pairing] Pairing completed. Starting the Agent0 receiver.

:start
echo [RemoteAgent-0] Starting the remote signal receiver.
echo [RemoteAgent-0] WAITING_SIGNAL - RemoteAgent owns its remote lifecycle and will not start LocalAgent.
"%SMARTAGENT_PYTHON%" -m agent_core.host_supervisor --remote-only
if errorlevel 1 goto failed
goto done

:failed
echo.
echo [RemoteAgent-0] Startup failed with exit code %errorlevel%.
pause

:launcher_self_test
echo REMOTE_AGENT_LAUNCHER_PARSE_OK
goto done

:done
endlocal
