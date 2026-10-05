@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent ACL Mode

if /i "%~1"=="on" goto valid_mode
if /i "%~1"=="off" goto valid_mode
echo Usage: ACLstatus.bat on^|off
exit /b 2

:valid_mode
if not "%~2"=="" (
    echo Usage: ACLstatus.bat on^|off
    exit /b 2
)
set "SMARTAGENT_ACL_MODE=%~1"
set "SMARTAGENT_ACL_SCRIPT=%~dp0install_smart_agent\acl_status.ps1"
set "SMARTAGENT_ACL_ROOT=%~dp0."
if not exist "%SMARTAGENT_ACL_SCRIPT%" (
    echo [ERROR] ACL mode manager not found: %SMARTAGENT_ACL_SCRIPT%
    exit /b 2
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$env:SMARTAGENT_ACL_CALLER_SID=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $quote=[char]34; $scriptPath=$quote+$env:SMARTAGENT_ACL_SCRIPT+$quote; $rootPath=$quote+$env:SMARTAGENT_ACL_ROOT+$quote; $p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$scriptPath,'-Mode',$env:SMARTAGENT_ACL_MODE,'-ProjectRoot',$rootPath,'-ControllerSid',$env:SMARTAGENT_ACL_CALLER_SID) -WorkingDirectory $env:SMARTAGENT_ACL_ROOT -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
set "ACL_EXIT=%ERRORLEVEL%"
if not "%ACL_EXIT%"=="0" (
    echo [SmartAgent] ACL mode change failed or was cancelled. Exit=%ACL_EXIT%
    exit /b %ACL_EXIT%
)
exit /b 0
