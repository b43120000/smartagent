@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title RemoteAgent - Local Telegram Sender Test

set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" set "SMARTAGENT_PYTHON=python"

echo ============================================================
echo RemoteAgent Local Telegram Sender Test
echo ============================================================
echo This test does not connect to Telegram.
echo Keep launch_remote_agent.bat running at WAITING_SIGNAL.
echo The request will use the saved Edit_Remoteworkspace.bat binding.
echo.

"%SMARTAGENT_PYTHON%" -m RemoteAgent.local_telegram_sender %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [RemoteAgent Test] Failed or timed out with exit code %RESULT%.
pause
exit /b %RESULT%
