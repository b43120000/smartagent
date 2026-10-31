@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "WEBAGENT_PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%WEBAGENT_PYTHON%" (
  echo [SR-GATE][ERROR] SmartAgent .venv Python is missing.
  exit /b 2
)
"%WEBAGENT_PYTHON%" -c "import sys;assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] SmartAgent project Python must be 3.10+.
  exit /b 2
)
rem FIRST READBACK ONLY. Shared WebGPT runtime, WebGPT1 as test entry.
rem No protocol repair, Narrative Bridge, GAR, remote planner or Tool Action.
set "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=1"
set "SMARTAGENT_SR_EVIDENCE_DIR=%CD%\localdata\logs\sr-first-readback"
set "SMARTAGENT_GAR_V1=0"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL="
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
echo ============================================================
echo  SR First-Readback-Only WebGPT1 Diagnostic Gate
echo ============================================================
echo SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=%SMARTAGENT_SR_FIRST_READBACK_ONLY_V1%
echo SMARTAGENT_GAR_V1=%SMARTAGENT_GAR_V1%
echo SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=%SMARTAGENT_GAR_D1_FAULT_INJECTION_V1%
echo SMARTAGENT_GAR_FRESH_CONTEXT_URL=(disabled)
echo TRI_ONE_TASK_RESULT_V1=%TRI_ONE_TASK_RESULT_V1%
echo TRI_ONE_TEXT_ONLY_V1=%TRI_ONE_TEXT_ONLY_V1%
echo SmartAgent Python: %WEBAGENT_PYTHON%
echo Evidence: %SMARTAGENT_SR_EVIDENCE_DIR%
echo.
echo [SR-GATE] No local Tool Action is executed. No repair or final ACK is sent.
echo [SR-GATE] After READY: SR-04 once, SR-05 identical prompt TWICE.
echo [SR-GATE] Wait for CAPTURED before the next request.
echo [SR-GATE] Use collect_sr_first_readback.bat for one ZIP.
echo.
call "%~dp0launch_webcopilot.bat"
set "RC=%ERRORLEVEL%"
exit /b %RC%
