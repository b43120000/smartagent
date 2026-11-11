@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent ACL OFF - Read Only Audit
set "AUDIT=%~dp0install_smart_agent\verify_acl_off.ps1"
if not exist "%AUDIT%" (
  echo [ERROR] Missing audit script: %AUDIT%
  exit /b 2
)
echo [SmartAgent] Running read-only ACL OFF audit...
echo [SmartAgent] This does not change ACL mode, permissions, accounts or tasks.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%AUDIT%" -ProjectRoot "%~dp0." %*
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="1" echo [SmartAgent] FAIL - some ACL OFF postconditions did not hold.
if "%RESULT%"=="2" echo [SmartAgent] INCOMPLETE - some conditions could not be proven.
echo [SmartAgent] Exit Code: %RESULT%
echo [SmartAgent] For a real FULL ACL OFF acceptance, a scoped OS verification and write probe are still needed.
pause
endlocal & exit /b %RESULT%
