@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 exit /b 2
rem P2 NORMAL Runtime, never First Readback controlled stop.
set "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=0"
set "SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1=1"
rem Exactly one eligible (>512 chars) ChatGPT Composer write per scraper
rem uses protected prefix and fast tail. Fail closed; never replay on failure.
set "SMARTAGENT_COMPOSER_HYBRID_ONCE_V1=1"
echo ============================================================
echo  P2 Hybrid Writer + Readonly Diagnostic (NORMAL Runtime)
echo ============================================================
echo [P2] First Readback Only=OFF
echo [P2] Readonly Evidence=ON
echo [P2] Hybrid Writer=ONCE only; input >512 chars
echo [P2] No protocol/turn admission bypass. Normal task tools may run.
echo [P2] Start one benign task; then collect_sr_first_readback.bat.
echo.
call "%~dp0launch_webcopilot.bat" %*
set "RC=%ERRORLEVEL%"
exit /b %RC%
