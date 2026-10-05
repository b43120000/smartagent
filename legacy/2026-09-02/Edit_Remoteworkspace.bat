@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - Remote Workspace + Telegram

set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" set "SMARTAGENT_PYTHON=python"

echo RemoteAgent workspace registry
echo.
echo Current configuration:
"%SMARTAGENT_PYTHON%" RemoteAgent\remote_workspace.py list
echo.

set /p "TARGET_WORKSPACE=Workspace path: "
if "%TARGET_WORKSPACE%"=="" goto done
if not exist "%TARGET_WORKSPACE%\." (
    echo [ERROR] Workspace does not exist.
    goto done
)

set /p "TARGET_URL=ChatGPT conversation URL: "
if "%TARGET_URL%"=="" goto done

"%SMARTAGENT_PYTHON%" RemoteAgent\remote_workspace.py set --workspace "%TARGET_WORKSPACE%" --url "%TARGET_URL%"
if errorlevel 1 (
    echo [ERROR] Workspace binding failed. Telegram setup was not started.
    goto done
)

echo.
echo [OK] Conversation and workspace are linked.
echo [Telegram] Pairing authorizes your Telegram user/chat globally; it is NOT repeated per workspace.
echo [Telegram] This run will route Telegram tasks to: %TARGET_WORKSPACE%
echo.

set "SMARTAGENT_TELEGRAM_WORKSPACE=%TARGET_WORKSPACE%"
set "SMARTAGENT_TELEGRAM_ENABLED=1"
set "SMARTAGENT_TELEGRAM_PAIRING_ENABLED=1"

if "%SMARTAGENT_TELEGRAM_BOT_TOKEN%"=="" (
    echo Telegram Bot Token is required for each Agent0 process, but it will not be saved to disk.
    set /p "SMARTAGENT_TELEGRAM_BOT_TOKEN=Paste BotFather token: "
)
if "%SMARTAGENT_TELEGRAM_BOT_TOKEN%"=="" (
    echo [ERROR] Telegram Bot Token was not provided.
    goto done
)

echo [Telegram] Saving Bot Token with Windows current-user encryption...
"%SMARTAGENT_PYTHON%" -m RemoteAgent.local_telegram_config save --workspace "%TARGET_WORKSPACE%"
if errorlevel 1 (
    echo [ERROR] Telegram encrypted local configuration could not be saved.
    goto done
)
echo [OK] One-click RemoteAgent startup is configured. Future runs use launch_remote_agent.bat.

if exist ".agents\remote_telegram_pairing.json" (
    findstr /c:"user_id" ".agents\remote_telegram_pairing.json" >nul 2>&1
    if not errorlevel 1 goto paired
)

echo [Telegram] No completed pairing found. Starting one-time QR pairing...
call launch_remote_agent.bat telegram-pair
goto done

:paired
echo [Telegram] Existing Telegram pairing found. QR pairing is skipped.
echo [Telegram] Starting RemoteAgent-0 with the selected workspace...
call launch_remote_agent.bat
goto done

:done
echo.
pause
endlocal
