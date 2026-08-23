@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - Local + Hidden Remote Supervisor
set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    set "SMARTAGENT_PYTHON=python"
    echo [SmartAgent] 尚未建立專用 .venv，暫時沿用系統 Python。
    echo [SmartAgent] 建議先執行 install_smart_agent.bat 完成一鍵環境安裝。
)
if /I "%~1"=="configure" goto configure
if "%SMARTAGENT_AUTOSTART%"=="1" goto start

if exist ".agents\startup_preferences.json" (
    echo [SmartAgent] 2 秒內按 M 可修改 Session / 大腦 / 執行 Agent / Agent 2；否則沿用上次設定。
    choice /C SM /N /T 2 /D S /M "[S] 沿用上次設定  [M] 修改設定: "
    rem CHOICE can return 255 in some non-interactive/automation consoles.
    rem Only the exact value 2 means the user selected M.
    if errorlevel 2 if not errorlevel 3 goto configure
    goto start
)

:configure
"%SMARTAGENT_PYTHON%" smart_agent.py --configure-startup
goto done

:start
"%SMARTAGENT_PYTHON%" -m agent_core.host_supervisor

:done
endlocal
