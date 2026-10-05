@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem The downloaded release folder is the update source.
set "SOURCE_ROOT=%~dp0."
set "TARGET_ROOT=%~1"
if not defined TARGET_ROOT set /p "TARGET_ROOT=Installed SmartAgent full path: "
if not defined TARGET_ROOT (
    echo [SmartAgent Update] ERROR Target path is required.
    exit /b 2
)

set "UPDATE_SCRIPT=%SOURCE_ROOT%\install_smart_agent\manifest_update.ps1"
set "MANIFEST_BUILDER=%SOURCE_ROOT%\install_smart_agent\build_update_manifest.py"
set "UPDATE_PYTHON=%TARGET_ROOT%\.venv\Scripts\python.exe"
set "SYSTEM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

title SmartAgent Update
echo [SmartAgent Update]
echo Source: %SOURCE_ROOT%
echo Target: %TARGET_ROOT%

if not exist "%UPDATE_SCRIPT%" (
    echo [SmartAgent Update] ERROR Missing updater: %UPDATE_SCRIPT%
    exit /b 2
)
if not exist "%MANIFEST_BUILDER%" (
    echo [SmartAgent Update] ERROR Missing manifest builder: %MANIFEST_BUILDER%
    exit /b 2
)
if not exist "%UPDATE_PYTHON%" (
    echo [SmartAgent Update] ERROR Missing installed Python: %UPDATE_PYTHON%
    echo [SmartAgent Update] Run install_smart_agent.bat in the target package first.
    exit /b 2
)
if not exist "%SYSTEM_POWERSHELL%" (
    echo [SmartAgent Update] ERROR Missing Windows PowerShell: %SYSTEM_POWERSHELL%
    exit /b 2
)

echo [SmartAgent Update] Rebuilding source protocol and release manifests...
set "PYTHONPATH=%SOURCE_ROOT%\source"
"%UPDATE_PYTHON%" -c "from pathlib import Path; from agent_core.protocol_manifest import write_protocol_manifest, validate_protocol_manifest; root=Path(r'%SOURCE_ROOT%'); write_protocol_manifest(root); validate_protocol_manifest(root)"
if errorlevel 1 (
    echo [SmartAgent Update] ERROR Failed to rebuild protocol manifest.
    exit /b 2
)
"%UPDATE_PYTHON%" "%MANIFEST_BUILDER%" --root "%SOURCE_ROOT%"
if errorlevel 1 (
    echo [SmartAgent Update] ERROR Failed to rebuild update manifest.
    exit /b 2
)

"%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%UPDATE_SCRIPT%" -SourceRoot "%SOURCE_ROOT%" -InstallRoot "%TARGET_ROOT%"
set "UPDATE_EXIT=%errorlevel%"
if "%UPDATE_EXIT%"=="0" (
    echo.
    echo [SmartAgent Update] Update completed successfully.
) else (
    echo.
    echo [SmartAgent Update] Update failed. exit_code=%UPDATE_EXIT%
)
endlocal & exit /b %UPDATE_EXIT%
