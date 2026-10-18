@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title SmartAgent - 9. blockingState Calibration
if /I "%~1"=="--launcher-self-test" (
    echo SMARTAGENT_COMPONENT_STAGE_9_LAUNCHER_OK
    exit /b 0
)
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [componentAdapter] Missing installed Python: %PYTHON_EXE%
    exit /b 2
)
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
echo [componentAdapter] Stage 9: blockingState - Select DOM and verify on website
"%PYTHON_EXE%" -B -m agent_core.web_ui.dialogue_calibration --stage 9 %*
set "CALIBRATION_EXIT=%errorlevel%"
echo [componentAdapter] exit_code=%CALIBRATION_EXIT%
if not defined SMARTAGENT_TEST_NO_PAUSE pause
endlocal & exit /b %CALIBRATION_EXIT%
