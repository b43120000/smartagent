@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 (
  echo [SR-GATE][ERROR] Cannot enter SmartAgent root.
  exit /b 2
)
if not exist "%~dp0launch_webcopilot.bat" (
  echo [SR-GATE][ERROR] Missing launch_webcopilot.bat.
  exit /b 2
)
rem This gate exercises ONLY the original Planner and shared runtime.
rem Never activate GAR, open L4 target, or inject malformed V9.
set "SMARTAGENT_GAR_A0_DIAGNOSTIC=0"
set "SMARTAGENT_GAR_V1=0"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL="
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
echo.
echo ============================================================
echo Shared Runtime SR-02 / SR-03 - WebGPT1 ONLY
echo ============================================================
echo GAR=%SMARTAGENT_GAR_V1%
echo D1_FAULT_INJECTION=%SMARTAGENT_GAR_D1_FAULT_INJECTION_V1%
echo FRESH_CONTEXT_URL=(disabled)
echo TRI_ONE_TASK_RESULT=%TRI_ONE_TASK_RESULT_V1%
echo TRI_ONE_TEXT_ONLY=%TRI_ONE_TEXT_ONLY_V1%
echo No synthetic protocol error is injected.
echo.
where git >nul 2>&1
if not errorlevel 1 git rev-parse HEAD
echo.
echo [SR-GATE] After READY, submit SR-02 and SR-03 separately.
echo [SR-GATE] Test instructions: doc\tests\shared_runtime_sr02_sr03.md
echo [SR-GATE] Do not submit another request until the prior one is terminal.
echo.
call "%~dp0launch_webcopilot.bat"
set "RC=%ERRORLEVEL%"
echo [SR-GATE] Controller exited with code %RC%.
exit /b %RC%
