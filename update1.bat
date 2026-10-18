@echo off
setlocal
chcp 65001 >nul 2>&1

rem SmartAgent E-drive update contract:
rem   source  = E:\card\release\SmartAgentv2
rem   runtime = E:\card\release\SmartAgentv2_1
rem Stop, update, and restart all operate on the E-drive runtime package.

set "SOURCE_ROOT=E:\card\release\SmartAgentv2"
set "TARGET_ROOT=E:\card\release\SmartAgentv2_1"
set "UPDATE_SCRIPT=%SOURCE_ROOT%\install_smart_agent\manifest_update.ps1"
set "MANIFEST_BUILDER=%SOURCE_ROOT%\install_smart_agent\build_update_manifest.py"
set "UPDATE_PYTHON=%TARGET_ROOT%\.venv\Scripts\python.exe"
set "STOP_SCRIPT=%TARGET_ROOT%\force_stop_all_agents.bat"
set "LAUNCH_SCRIPT=%TARGET_ROOT%\launch_remote_agent.bat"

title SmartAgent - Update E Drive Runtime From Development Source

echo [SmartAgent Update1]
echo Source: %SOURCE_ROOT%
echo Target: %TARGET_ROOT%
call E:\card\release\SmartAgentv2\close_web_pages.bat
if not exist "%TARGET_ROOT%\" (
    echo [SmartAgent Update1] ERROR Missing target package: %TARGET_ROOT%
    exit /b 2
)
if not exist "%STOP_SCRIPT%" (
    echo [SmartAgent Update1] ERROR Missing stop launcher: %STOP_SCRIPT%
    exit /b 2
)
if not exist "%LAUNCH_SCRIPT%" (
    echo [SmartAgent Update1] ERROR Missing runtime launcher: %LAUNCH_SCRIPT%
    exit /b 2
)
if not exist "%UPDATE_SCRIPT%" (
    echo [SmartAgent Update1] ERROR Missing updater: %UPDATE_SCRIPT%
    exit /b 2
)
if not exist "%MANIFEST_BUILDER%" (
    echo [SmartAgent Update1] ERROR Missing manifest builder: %MANIFEST_BUILDER%
    exit /b 2
)
if not exist "%UPDATE_PYTHON%" (
    echo [SmartAgent Update1] ERROR Missing installed Python: %UPDATE_PYTHON%
    echo [SmartAgent Update1] Run install_smart_agent.bat for the E-drive target first.
    exit /b 2
)

echo [SmartAgent Update1] Stopping E-drive runtime...
call "%STOP_SCRIPT%" --no-pause
if errorlevel 1 (
    echo [SmartAgent Update1] ERROR Failed to stop E-drive runtime.
    exit /b 2
)

echo [SmartAgent Update1] Rebuilding source protocol and release manifests...
set "PYTHONPATH=%SOURCE_ROOT%\source"
"%UPDATE_PYTHON%" -c "from pathlib import Path; from agent_core.protocol_manifest import write_protocol_manifest, validate_protocol_manifest; root=Path(r'%SOURCE_ROOT%'); write_protocol_manifest(root); validate_protocol_manifest(root)"
if errorlevel 1 (
    echo [SmartAgent Update1] ERROR Failed to rebuild protocol manifest.
    exit /b 2
)
"%UPDATE_PYTHON%" "%MANIFEST_BUILDER%" --root "%SOURCE_ROOT%"
if errorlevel 1 (
    echo [SmartAgent Update1] ERROR Failed to rebuild update manifest.
    exit /b 2
)

set "SYSTEM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%SYSTEM_POWERSHELL%" (
    echo [SmartAgent Update1] ERROR Missing Windows PowerShell: %SYSTEM_POWERSHELL%
    exit /b 2
)

"%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%UPDATE_SCRIPT%" -SourceRoot "%SOURCE_ROOT%" -InstallRoot "%TARGET_ROOT%" %*
set "UPDATE_EXIT=%errorlevel%"

if "%UPDATE_EXIT%"=="0" (
    echo.
    echo [SmartAgent Update1] Update completed successfully.
) else (
    echo.
    echo [SmartAgent Update1] Update failed. exit_code=%UPDATE_EXIT%
)

echo [SmartAgent Update1] Starting E-drive runtime...
call "%LAUNCH_SCRIPT%"
set "LAUNCH_EXIT=%errorlevel%"
if not "%LAUNCH_EXIT%"=="0" (
    echo [SmartAgent Update1] ERROR E-drive runtime launch failed. exit_code=%LAUNCH_EXIT%
    if "%UPDATE_EXIT%"=="0" set "UPDATE_EXIT=%LAUNCH_EXIT%"
)

endlocal & exit /b %UPDATE_EXIT%
