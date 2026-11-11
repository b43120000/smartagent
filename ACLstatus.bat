@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /I "%~1"=="--launcher-self-test" (
 echo ACLSTATUS_LAUNCHER_PARSE_OK
 exit /b 0
)
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent ACL Mode

if /i "%~1"=="on" goto valid_mode
if /i "%~1"=="off" goto valid_mode
if /i "%~1"=="status" goto status_only
echo Usage: ACLstatus.bat on^|off^|status
exit /b 2

:valid_mode
if not "%~2"=="" (
    echo Usage: ACLstatus.bat on^|off^|status
    exit /b 2
)
set "SMARTAGENT_ACL_MODE=%~1"
set "SMARTAGENT_ACL_ROOT=%~dp0."
set "SMARTAGENT_ACL_COMMAND=%~dp0install_smart_agent\acl_mode_command.ps1"
if not exist "%SMARTAGENT_ACL_COMMAND%" (
    echo [SmartAgent ACL] FAIL: internal ACL terminal command is missing.
    echo [SmartAgent ACL] File: %SMARTAGENT_ACL_COMMAND%
    exit /b 2
)
set "SMARTAGENT_ACL_LOG_DIR=%LOCALAPPDATA%\SmartAgent\acl_status"
if not exist "%SMARTAGENT_ACL_LOG_DIR%" mkdir "%SMARTAGENT_ACL_LOG_DIR%" >nul 2>&1
if not exist "%SMARTAGENT_ACL_LOG_DIR%" (
    echo [SmartAgent ACL] FAIL: cannot create terminal log directory.
    exit /b 2
)
set "ACL_RUN_ID="
for /f %%G in ('"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "[guid]::NewGuid().ToString('N')"') do set "ACL_RUN_ID=%%G"
if not defined ACL_RUN_ID (
    echo [SmartAgent ACL] FAIL: unable to allocate result identity.
    exit /b 2
)
set "SMARTAGENT_ACL_RECEIPT=%SMARTAGENT_ACL_LOG_DIR%\acl_%SMARTAGENT_ACL_MODE%_%ACL_RUN_ID%.json"
set "SMARTAGENT_ACL_TRANSCRIPT=%SMARTAGENT_ACL_LOG_DIR%\acl_%SMARTAGENT_ACL_MODE%_%ACL_RUN_ID%.txt"
echo.
echo [SmartAgent ACL] Starting %SMARTAGENT_ACL_MODE%...
echo [SmartAgent ACL] This command changes Windows permissions and restricted Executor state.
echo [SmartAgent ACL] The final result and any original error will appear in THIS window.

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $env:SMARTAGENT_ACL_CALLER_SID=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $q=[char]34; $s=$q+$env:SMARTAGENT_ACL_COMMAND+$q; $root=$q+$env:SMARTAGENT_ACL_ROOT+$q; $receipt=$q+$env:SMARTAGENT_ACL_RECEIPT+$q; $trace=$q+$env:SMARTAGENT_ACL_TRANSCRIPT+$q; $p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$s,'-Mode',$env:SMARTAGENT_ACL_MODE,'-ProjectRoot',$root,'-ControllerSid',$env:SMARTAGENT_ACL_CALLER_SID,'-ReceiptPath',$receipt,'-TranscriptPath',$trace) -WorkingDirectory $env:SMARTAGENT_ACL_ROOT -Verb RunAs -Wait -PassThru; exit $p.ExitCode } catch { Write-Host ('[SmartAgent ACL] Elevation/launch failed: '+$_.Exception.Message) -ForegroundColor Red; exit 3 }"
set "ACL_CHILD_EXIT=%ERRORLEVEL%"

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SMARTAGENT_ACL_COMMAND%" -PrintReceipt -Mode "%SMARTAGENT_ACL_MODE%" -ProjectRoot "%SMARTAGENT_ACL_ROOT%" -ReceiptPath "%SMARTAGENT_ACL_RECEIPT%" -ProcessExitCode %ACL_CHILD_EXIT%
set "ACL_DISPLAY_EXIT=%ERRORLEVEL%"
echo.
echo [SmartAgent ACL] Current shared ACL/Turbo state:
call :status_print
if not "%ACL_CHILD_EXIT%"=="0" (
    echo [SmartAgent ACL] Operation FAILED. Elevated exit=%ACL_CHILD_EXIT%.
    exit /b %ACL_CHILD_EXIT%
)
if not "%ACL_DISPLAY_EXIT%"=="0" (
    echo [SmartAgent ACL] Operation FAILED: result missing or postcondition mismatch.
    exit /b 1
)
echo [SmartAgent ACL] %SMARTAGENT_ACL_MODE% operation completed successfully.
exit /b 0

:status_only
if not "%~2"=="" (
 echo Usage: ACLstatus.bat on^|off^|status
 exit /b 2
)
call :status_print
exit /b %ERRORLEVEL%

:status_print
if not exist "%~dp0install_smart_agent\turbo_shared_status.ps1" (
 echo [SmartAgent ACL] Shared status reporter missing.
 exit /b 2
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_smart_agent\turbo_shared_status.ps1" -ProjectRoot "%~dp0."
exit /b %ERRORLEVEL%
