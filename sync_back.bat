@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent - Sync Installed Changes Back To Source

if /I "%~1"=="--launcher-self-test" (
    echo SMARTAGENT_SYNC_BACK_LAUNCHER_PARSE_OK
    if not "%~2"=="" echo SMARTAGENT_SYNC_BACK_DESTINATION=%~2
    exit /b 0
)

set "SYNC_SCRIPT=%~dp0install_smart_agent\sync_back.ps1"
if not exist "%SYNC_SCRIPT%" (
    echo [SmartAgent Sync Back] ERROR Missing script: %SYNC_SCRIPT%
    exit /b 2
)

set "SYSTEM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%SYSTEM_POWERSHELL%" (
    echo [SmartAgent Sync Back] ERROR Missing Windows PowerShell: %SYSTEM_POWERSHELL%
    exit /b 2
)

set "SYNC_DESTINATION=%~1"
if not defined SYNC_DESTINATION goto invoke_interactive_or_switches
if "%SYNC_DESTINATION:~0,1%"=="-" goto invoke_interactive_or_switches

"%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SYNC_SCRIPT%" -InstalledRoot "%~dp0." -DestinationRoot "%SYNC_DESTINATION%" %2 %3 %4 %5 %6 %7 %8 %9
goto sync_finished

:invoke_interactive_or_switches
"%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SYNC_SCRIPT%" -InstalledRoot "%~dp0." %*

:sync_finished
set "SYNC_EXIT=%errorlevel%"
if "%SYNC_EXIT%"=="0" (
    echo.
    echo [SmartAgent Sync Back] Completed successfully.
) else (
    echo.
    echo [SmartAgent Sync Back] Failed. exit_code=%SYNC_EXIT%
)
pause
endlocal & exit /b %SYNC_EXIT%
