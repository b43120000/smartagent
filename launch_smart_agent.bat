@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - LocalAgent
set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    set "SMARTAGENT_PYTHON=python"
    echo [SmartAgent] Project .venv was not found; using Python from PATH.
    echo [SmartAgent] Run install_smart_agent.bat first if dependencies are missing.
)
if /I "%~1"=="configure" goto configure
if /I "%~1"=="telegram-pair" goto remote_pair_redirect
if /I "%~1"=="--launcher-self-test" goto launcher_self_test
if "%SMARTAGENT_AUTOSTART%"=="1" goto start

if exist ".agents\startup_preferences.json" (
    echo [SmartAgent] Press M within 2 seconds to change startup settings; otherwise reuse the saved settings.
    choice /C SM /N /T 2 /D S /M "[S] Reuse saved settings  [M] Modify settings: "
    rem CHOICE can return 255 in some non-interactive/automation consoles.
    rem Only the exact value 2 means the user selected M.
    if errorlevel 2 if not errorlevel 3 goto configure
    goto start
)

:configure
"%SMARTAGENT_PYTHON%" smart_agent.py --configure-startup
goto done

:remote_pair_redirect
echo [LocalAgent] Telegram pairing belongs to RemoteAgent. Redirecting...
call launch_remote_agent.bat telegram-pair
goto done
:start
"%SMARTAGENT_PYTHON%" -m agent_core.host_supervisor --local-only
goto done

:launcher_self_test
echo SMART_AGENT_INTEGRATED_LAUNCHER_PARSE_OK
goto done

:done
endlocal
