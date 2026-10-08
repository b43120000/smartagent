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
echo [adapterUI] install_root=%~dp0
echo [adapterUI] python=%PYTHON_EXE%
echo [adapterUI] source=%~dp0source\agent_core\ui_calibration.py
echo [adapterUI] calibration_log=%~dp0localdata\logs\web_ui_calibration.jsonl
echo [adapterUI] build=E_PATCH_20261005

if not "%~1"=="" if not "%~2"=="" (
    echo [adapterUI] planner_url=%~1
    echo [adapterUI] target_url=%~2
    "%PYTHON_EXE%" -B -m agent_core.ui_calibration --planner-url "%~1" --target-url "%~2"
) else (
    "%PYTHON_EXE%" -B -m agent_core.ui_calibration %*
)
set "ADAPTER_EXIT=%errorlevel%"
if "%ADAPTER_EXIT%"=="0" (
    echo.
    echo [adapterUI] Calibration completed and activated.
    echo [adapterUI] RESULT=SUCCESS
) else (
    echo.
    echo [adapterUI] Calibration did not publish any profile. exit_code=%ADAPTER_EXIT%
    echo [adapterUI] RESULT=FAILED ^| exit_code=%ADAPTER_EXIT%
)
pause
endlocal & exit /b %ADAPTER_EXIT%
