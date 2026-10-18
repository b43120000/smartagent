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
set "PYTHONIOENCODING=utf-8"
if "%~1"=="" goto mode_menu
if /I "%~1"=="--manual" goto manual_cli
goto model_mode

:mode_menu
echo.
echo [1] Model Adapter Calibration
echo [2] manualAdapterUI - Select composer and verify input
echo [3] Exit
choice /C 123 /N /M "Select mode [1/2/3]: "
if errorlevel 3 exit /b 0
if errorlevel 2 goto manual_mode
goto model_mode

:manual_cli
"%PYTHON_EXE%" -B -m agent_core.web_ui.manual_adapter %2 %3 %4 %5 %6 %7 %8 %9
goto manual_done

:manual_mode
"%PYTHON_EXE%" -B -m agent_core.web_ui.composer_calibration
:manual_done
set "ADAPTER_EXIT=%errorlevel%"
echo [adapterUI] Manual composer workflow exit_code=%ADAPTER_EXIT%
if not defined SMARTAGENT_TEST_NO_PAUSE pause
endlocal & exit /b %ADAPTER_EXIT%

:model_mode
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
if not defined SMARTAGENT_TEST_NO_PAUSE pause
endlocal & exit /b %ADAPTER_EXIT%

