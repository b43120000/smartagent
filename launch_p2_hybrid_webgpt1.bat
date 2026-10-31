@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 exit /b 2
rem P2 NORMAL Runtime, never First Readback controlled stop.
set "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=0"
set "SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1=1"
rem First eligible (>512 chars) real task Planner write after READY only;
rem Session Bootstrap/Attach and final ACK do not consume the grant.
set "SMARTAGENT_COMPOSER_HYBRID_ONCE_V1=1"
rem Enable existing Phase 5A answer-only receipt for text-only requests.
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
set "TRI_ONE_TEXT_ONLY_WEB_DIRECT=1"
echo ============================================================
echo  P2 Hybrid Writer + Readonly Diagnostic (NORMAL Runtime)
echo ============================================================
echo [P2] First Readback Only=OFF
echo [P2] Readonly Evidence=ON
echo [P2] Tri-One 5A Answer-only=ON for WEB_DIRECT
echo [P2] Hybrid Writer=ONCE after READY, first real task Planner >512 chars
echo [P2] No protocol/turn admission bypass. Normal task tools may run.
echo [P2] Start one benign task; then collect_sr_first_readback.bat.
echo.
call "%~dp0launch_webcopilot.bat" %*
set "RC=%ERRORLEVEL%"
exit /b %RC%
