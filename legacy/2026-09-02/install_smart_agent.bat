@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title SmartAgent Environment Check and Installer

set "HELPER_DIR=%~dp0install_smart_agent"
set "CHECKER=%HELPER_DIR%\check_environment.ps1"
set "INSTALLER=%HELPER_DIR%\install.ps1"
set "REPORT=%HELPER_DIR%\environment_report.txt"
set "SMARTAGENT_INSTALLER_DIR=%HELPER_DIR%"
set "VALIDATE_ONLY=0"
set "NON_INTERACTIVE=0"

for %%A in (%*) do (
    if /I "%%~A"=="-ValidateOnly" set "VALIDATE_ONLY=1"
    if /I "%%~A"=="-NonInteractive" set "NON_INTERACTIVE=1"
)

if not exist "%CHECKER%" (
    echo [ERROR] Environment checker not found:
    echo         %CHECKER%
    echo.
    echo The release package is incomplete. Re-extract or download it again.
    pause
    exit /b 2
)

if not exist "%INSTALLER%" (
    echo [ERROR] Installer helper not found:
    echo         %INSTALLER%
    echo.
    echo The release package is incomplete. Re-extract or download it again.
    pause
    exit /b 2
)

echo ============================================================
echo  SmartAgent environment check
echo ============================================================
echo.

call :run_check %*
set "CHECK_EXIT=%ERRORLEVEL%"

if "%CHECK_EXIT%"=="0" (
    echo.
    echo [SmartAgent] All required environments passed. Installation is not needed.
    echo [SmartAgent] Diagnostic report: %REPORT%
    if "%NON_INTERACTIVE%"=="0" pause
    exit /b 0
)

if "%CHECK_EXIT%"=="2" (
    echo.
    echo [SmartAgent] The environment checker could not complete.
    echo [SmartAgent] Diagnostic report: %REPORT%
    if "%NON_INTERACTIVE%"=="0" pause
    exit /b 2
)

echo.
echo [SmartAgent] One or more required environments are missing or broken.
echo [SmartAgent] Review the list above or open:
echo              %REPORT%

if "%VALIDATE_ONLY%"=="1" (
    echo [SmartAgent] Validation-only mode: no installation was performed.
    if "%NON_INTERACTIVE%"=="0" pause
    exit /b 1
)

if "%NON_INTERACTIVE%"=="0" (
    echo.
    set /p "CONFIRM=Press Enter to install/repair the failed items, or close this window to cancel: "
)

echo.
echo ============================================================
echo  SmartAgent install / repair
echo ============================================================
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "try { $source = Get-Content -LiteralPath '%INSTALLER%' -Raw -Encoding UTF8; & ([ScriptBlock]::Create($source)) %*; exit $LASTEXITCODE } catch { Write-Error $_; exit 1 }"
set "INSTALL_EXIT=%ERRORLEVEL%"

if not "%INSTALL_EXIT%"=="0" (
    echo.
    echo [SmartAgent] Installation did not complete.
    echo [SmartAgent] Install report: %HELPER_DIR%\install_report.txt
    if "%NON_INTERACTIVE%"=="0" pause
    exit /b %INSTALL_EXIT%
)

echo.
echo ============================================================
echo  SmartAgent post-install verification
echo ============================================================
call :run_check %*
set "FINAL_EXIT=%ERRORLEVEL%"

if "%FINAL_EXIT%"=="0" (
    echo.
    echo [SmartAgent] Installation and final environment verification passed.
) else (
    echo.
    echo [SmartAgent] Installation finished, but one or more checks still failed.
    echo [SmartAgent] Diagnostic report: %REPORT%
)

if "%NON_INTERACTIVE%"=="0" pause
exit /b %FINAL_EXIT%

:run_check
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CHECKER%" %*
exit /b %ERRORLEVEL%
