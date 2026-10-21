@echo off
for %%R in ("%~dp0..\..") do set "SMARTAGENT_PACKAGE_ROOT=%%~fR\"
setlocal
chcp 65001 >nul 2>&1
cd /d "%SMARTAGENT_PACKAGE_ROOT%"

set "RESTART_SCRIPT=%SMARTAGENT_PACKAGE_ROOT%install_smart_agent\launchers\restart_chatgpt.ps1"
set "SYSTEM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%RESTART_SCRIPT%" (
    echo [Restart ChatGPT] ERROR Missing helper: %RESTART_SCRIPT%
    exit /b 2
)
if not exist "%SYSTEM_POWERSHELL%" (
    echo [Restart ChatGPT] ERROR Missing Windows PowerShell: %SYSTEM_POWERSHELL%
    exit /b 2
)

if /I "%~1"=="--self-test" (
    "%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RESTART_SCRIPT%" -SelfTest
    exit /b %errorlevel%
)

echo [Restart ChatGPT] ChatGPT will close and restart in 2 seconds.
start "SmartAgent Restart ChatGPT" /min "%SYSTEM_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RESTART_SCRIPT%" -DelaySeconds 2
if errorlevel 1 (
    echo [Restart ChatGPT] ERROR Unable to start restart helper.
    exit /b 2
)

echo [Restart ChatGPT] Restart helper started.
endlocal & exit /b 0

