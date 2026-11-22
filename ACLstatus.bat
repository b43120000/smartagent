@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent ACL Mode

if /I "%~1"=="--launcher-self-test" goto launcher_self_test
if /I "%~1"=="on" goto valid_mode
if /I "%~1"=="off" goto valid_mode
if /I "%~1"=="status" goto status_only
echo Usage: ACLstatus.bat on^|off^|status
exit /b 2

:launcher_self_test
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_smart_agent\acl_mode_launcher.ps1" -SelfTest
exit /b %ERRORLEVEL%

:valid_mode
if not "%~2"=="" (
    echo Usage: ACLstatus.bat on^|off^|status
    exit /b 2
)
if not exist "%~dp0install_smart_agent\acl_mode_launcher.ps1" (
    echo [SmartAgent ACL] RESULT: FAIL - internal launcher missing.
    exit /b 2
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_smart_agent\acl_mode_launcher.ps1" -Mode "%~1" -ProjectRoot "%~dp0."
exit /b %ERRORLEVEL%

:status_only
if not "%~2"=="" (
    echo Usage: ACLstatus.bat on^|off^|status
    exit /b 2
)
if not exist "%~dp0install_smart_agent\turbo_shared_status.ps1" (
    echo [SmartAgent ACL] Shared status reporter missing.
    exit /b 2
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_smart_agent\turbo_shared_status.ps1" -ProjectRoot "%~dp0."
exit /b %ERRORLEVEL%
