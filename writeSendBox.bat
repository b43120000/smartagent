@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - writeSendBox
if /I "%~1"=="--launcher-self-test" (
    echo SMARTAGENT_WRITE_SEND_BOX_LAUNCHER_OK
    exit /b 0
)
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [writeSendBox] Missing installed Python: %PYTHON_EXE%
    exit /b 2
)
set "PYTHONPATH=%~dp0source"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
"%PYTHON_EXE%" -B -m agent_core.web_ui.tests.write_send_box %*
set "TEST_EXIT=%errorlevel%"
echo [writeSendBox] exit_code=%TEST_EXIT%
if not defined SMARTAGENT_TEST_NO_PAUSE pause
endlocal & exit /b %TEST_EXIT%
