@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent Public Installer
if not exist "%~dp0localdata\" (
    mkdir "%~dp0localdata" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Unable to create SmartAgent runtime data directory: %~dp0localdata
        pause
        exit /b 2
    )
)
set "BOOTSTRAP=%~dp0install_smart_agent\bootstrap.ps1"
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
