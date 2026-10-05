@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - Web UI Adapter Calibration

if /I "%~1"=="--launcher-self-test" (
    echo SMARTAGENT_ADAPTER_UI_LAUNCHER_PARSE_OK
    exit /b 0
)

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [adapterUI] ERROR Missing installed Python: %PYTHON_EXE%
    echo [adapterUI] Run this from an installed or prepared SmartAgent package.
    exit /b 2
)

set "PYTHONPATH=%~dp0source"
set "PYTHONDONTWRITEBYTECODE=1"
"%PYTHON_EXE%" -B -m agent_core.ui_calibration %*
set "ADAPTER_EXIT=%errorlevel%"
if "%ADAPTER_EXIT%"=="0" (
    echo.
    echo [adapterUI] Calibration completed and activated.
) else (
    echo.
    echo [adapterUI] Calibration did not publish any profile. exit_code=%ADAPTER_EXIT%
)
pause
endlocal & exit /b %ADAPTER_EXIT%
