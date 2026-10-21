@echo off
for %%R in ("%~dp0..\..") do set "SMARTAGENT_PACKAGE_ROOT=%%~fR\"
setlocal
chcp 65001 >nul 2>&1
cd /d "%SMARTAGENT_PACKAGE_ROOT%"
title SmartAgent Full Reinstall Reset
if not exist "%SMARTAGENT_PACKAGE_ROOT%localdata\" (
    mkdir "%SMARTAGENT_PACKAGE_ROOT%localdata" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Unable to create SmartAgent runtime data directory: %SMARTAGENT_PACKAGE_ROOT%localdata
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

set "SMARTAGENT_REINSTALL_SCRIPT=%SMARTAGENT_PACKAGE_ROOT%install_smart_agent\reinstall.ps1"
set "SMARTAGENT_REINSTALL_ROOT=%SMARTAGENT_PACKAGE_ROOT%."
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$env:SMARTAGENT_REINSTALL_SCRIPT,'-SourceRoot',$env:SMARTAGENT_REINSTALL_ROOT) -WorkingDirectory $env:SMARTAGENT_REINSTALL_ROOT -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
set "RESET_EXIT=%ERRORLEVEL%"
if not "%RESET_EXIT%"=="0" (
    echo.
    echo [SmartAgent] Full reset failed or was cancelled. Exit=%RESET_EXIT%
    pause
    exit /b %RESET_EXIT%
)

echo.
echo [SmartAgent] Computer-wide authorization and local runtime were cleared.
echo [SmartAgent] Run install_smart_agent\launchers\install_smart_agent.bat manually when ready.
pause
exit /b 0
