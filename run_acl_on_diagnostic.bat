@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent ACL ON Diagnostic - Captured Execution
set "SMARTAGENT_DIAG_SCRIPT=%~dp0install_smart_agent\run_acl_on_with_diagnostics.ps1"
set "SMARTAGENT_DIAG_ROOT=%~dp0."
if not exist "%SMARTAGENT_DIAG_SCRIPT%" (echo [ERROR] Missing diagnostic runner & exit /b 2)
echo [WARNING] This launcher requests administrator elevation and can perform ACL ON.
echo [WARNING] Elevated script requires typing RUN-ACL-ON before making changes.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; $s=$q+$env:SMARTAGENT_DIAG_SCRIPT+$q; $r=$q+$env:SMARTAGENT_DIAG_ROOT+$q; try { $p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$s,'-ProjectRoot',$r) -WorkingDirectory $env:SMARTAGENT_DIAG_ROOT -Verb RunAs -Wait -PassThru; exit $p.ExitCode } catch { Write-Host ('Elevation or invocation failed: ' + $_.Exception.Message); exit 4 }"
set "CODE=%ERRORLEVEL%"
echo.
echo [SmartAgent] ACL ON diagnostic runner exit code: %CODE%
echo [SmartAgent] Logs: %%LOCALAPPDATA%%\SmartAgent\acl_on_diagnostics
pause
endlocal & exit /b %CODE%
