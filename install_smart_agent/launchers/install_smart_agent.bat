@echo off
for %%R in ("%~dp0..\..") do set "SMARTAGENT_PACKAGE_ROOT=%%~fR\"
setlocal
chcp 65001 >nul 2>&1
cd /d "%SMARTAGENT_PACKAGE_ROOT%"
title SmartAgent Public Installer
if not exist "%SMARTAGENT_PACKAGE_ROOT%localdata\" (
    mkdir "%SMARTAGENT_PACKAGE_ROOT%localdata" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Unable to create SmartAgent runtime data directory: %SMARTAGENT_PACKAGE_ROOT%localdata
        pause
        exit /b 2
    )
)
set "BOOTSTRAP=%SMARTAGENT_PACKAGE_ROOT%install_smart_agent\bootstrap.ps1"
if not exist "%BOOTSTRAP%" (
    echo [ERROR] Public installer bootstrap not found: %BOOTSTRAP%
    pause
    exit /b 2
)
echo ============================================================
echo  SmartAgent Two-Phase Installer
echo ============================================================
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP%" %*
set "INSTALL_EXIT=%ERRORLEVEL%"
if not "%INSTALL_EXIT%"=="0" (
    echo.
    echo [SmartAgent] Installation did not complete.
) else (
    echo.
    echo [SmartAgent] Installation complete.
    echo [SmartAgent] Run Edit_workspace.bat to choose the normal workspace and ChatGPT conversation.
)
if not defined SMARTAGENT_INSTALLER_NO_PAUSE pause
exit /b %INSTALL_EXIT%
