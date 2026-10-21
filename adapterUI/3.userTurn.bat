@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
call "%CD%\install_smart_agent\launchers\ensure_portable_runtime.bat"
if errorlevel 1 exit /b %ERRORLEVEL%
title SmartAgent - 3. userTurn Calibration
if /I "%~1"=="--launcher-self-test" (
    echo SMARTAGENT_COMPONENT_STAGE_3_LAUNCHER_OK
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
echo [componentAdapter] Stage 3: userTurn - Select DOM and verify on website
"%PYTHON_EXE%" -B -m agent_core.web_ui.component_calibration --stage 3 %*
set "CALIBRATION_EXIT=%errorlevel%"
echo [componentAdapter] exit_code=%CALIBRATION_EXIT%
if not defined SMARTAGENT_TEST_NO_PAUSE pause
endlocal & exit /b %CALIBRATION_EXIT%
