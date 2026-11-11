@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent ACL ON Diagnostic - Read Only
set "AUDIT=%~dp0install_smart_agent\diagnose_acl_on.ps1"
if not exist "%AUDIT%" (echo [ERROR] Missing %AUDIT% & exit /b 2)
echo [SmartAgent] READ-ONLY ACL ON postmortem (NO ACL changes).
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%AUDIT%" -ProjectRoot "%~dp0." %*
set "CODE=%ERRORLEVEL%"
echo.
echo [SmartAgent] Diagnostic exit code: %CODE%
echo [SmartAgent] A snapshot alone may not reveal the original PowerShell exception.
pause
endlocal & exit /b %CODE%
