@echo off

setlocal

chcp 65001 >nul 2>&1



rem SmartAgent normal update contract: development/source is authoritative.
rem This launcher never elevates. ACL mode changes are separate privileged operations.
rem Deployment is allowed only when update.ps1 verifies the installed runtime ACL mode is OFF.

rem Desktop\SmartAgent is only a launcher facade.

for %%I in ("%~dp0.") do set "SOURCE_ROOT=%%~fI"

set "TARGET_ROOT=%USERPROFILE%\Desktop\remoteagent\release\SmartAgentv2_1"

set "UPDATE_SCRIPT=%SOURCE_ROOT%\install_smart_agent\manifest_update.ps1"
set "MANIFEST_BUILDER=%SOURCE_ROOT%\install_smart_agent\build_update_manifest.py"
set "UPDATE_PYTHON=%TARGET_ROOT%\.venv\Scripts\python.exe"



title SmartAgent - Update Runtime From Development Source

echo [SmartAgent Update]

echo Source: %SOURCE_ROOT%

echo Target: %TARGET_ROOT%

echo close

call "%TARGET_ROOT%\force_stop_all_agents.bat"

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
    echo [SmartAgent Update] Run install_smart_agent.bat for the target first.
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



set "SYSTEM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%SYSTEM_POWERSHELL%" (

    echo [SmartAgent Update] ERROR Missing Windows PowerShell: %SYSTEM_POWERSHELL%

    exit /b 2

)



"%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%UPDATE_SCRIPT%" -SourceRoot "%SOURCE_ROOT%" -InstallRoot "%TARGET_ROOT%" %*

set "UPDATE_EXIT=%errorlevel%"



if "%UPDATE_EXIT%"=="0" (
    echo.
    echo [SmartAgent Update] Update completed successfully.
    echo open
    call "%TARGET_ROOT%\launch_remote_agent.bat"
) else (
    echo.
    echo [SmartAgent Update] Update failed. exit_code=%UPDATE_EXIT%
    echo open
    call "%TARGET_ROOT%\launch_remote_agent.bat"
)
endlocal & exit /b %UPDATE_EXIT%
