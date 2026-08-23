@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent Environment Installer
set "INSTALLER=%~dp0install_smart_agent\install.ps1"
set "SMARTAGENT_INSTALLER_DIR=%~dp0install_smart_agent"
if not exist "%INSTALLER%" (
    echo [ERROR] Installer helper not found: %INSTALLER%
    pause
    exit /b 2
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "try { $source = Get-Content -LiteralPath '%INSTALLER%' -Raw -Encoding UTF8; & ([ScriptBlock]::Create($source)) %*; exit $LASTEXITCODE } catch { Write-Error $_; exit 1 }"
set "INSTALL_EXIT=%ERRORLEVEL%"
if not "%INSTALL_EXIT%"=="0" (
    echo.
    echo [SmartAgent] Installation did not complete. See install_smart_agent\install_report.txt
) else (
    echo.
    echo [SmartAgent] Environment installation and validation completed.
)
pause
exit /b %INSTALL_EXIT%
