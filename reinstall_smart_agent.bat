@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent Full Reinstall Reset
if not exist "%~dp0localdata\" (
    mkdir "%~dp0localdata" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Unable to create SmartAgent runtime data directory: %~dp0localdata
        pause
        exit /b 2
    )
)

echo ============================================================
echo  SmartAgent Full Reinstall Reset
echo ============================================================
echo This revokes the computer-wide restricted-executor authorization.
echo Telegram pairing and user Workspace files are preserved.
echo A Windows administrator confirmation will follow.
echo.

set "SMARTAGENT_REINSTALL_SCRIPT=%~dp0install_smart_agent\reinstall.ps1"
set "SMARTAGENT_REINSTALL_ROOT=%~dp0."
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$quote=[char]34; $scriptPath=$quote+$env:SMARTAGENT_REINSTALL_SCRIPT+$quote; $rootPath=$quote+$env:SMARTAGENT_REINSTALL_ROOT+$quote; $p=Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$scriptPath,'-SourceRoot',$rootPath) -WorkingDirectory $env:SMARTAGENT_REINSTALL_ROOT -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
set "RESET_EXIT=%ERRORLEVEL%"
if not "%RESET_EXIT%"=="0" (
    echo.
    echo [SmartAgent] Full reset failed or was cancelled. Exit=%RESET_EXIT%
    pause
    exit /b %RESET_EXIT%
)

echo.
echo [SmartAgent] Computer-wide authorization and local runtime were cleared.
echo [SmartAgent] Run install_smart_agent.bat manually when ready.
pause
exit /b 0
