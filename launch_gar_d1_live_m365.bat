@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem D1 test-only launch. The original conversation URL comes from existing
rem WebCopilot startup preferences. Supply a DIFFERENT existing conversation
rem URL from the SAME provider, in the SAME browser profile.
cd /d "%~dp0"
if errorlevel 1 (
  echo [ERROR] Cannot enter the SmartAgent checkout folder.
  exit /b 2
)
if not exist "launch_webcopilot.bat" (
  echo [ERROR] Missing launch_webcopilot.bat. Put this file in the repo root.
  exit /b 2
)
echo.
echo [GAR D1] Use the FULL URL of WebGPT2 from your existing Copilot Cloud.
echo [GAR D1] WebGPT1 comes from the already saved WebCopilot startup.
echo [GAR D1] Both conversations must belong to the SAME provider.
echo [GAR D1] Do NOT enter the original WebGPT1 URL.
echo.
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL="
set /p "SMARTAGENT_GAR_FRESH_CONTEXT_URL=WebGPT2 existing conversation URL: "
if not defined SMARTAGENT_GAR_FRESH_CONTEXT_URL (
  echo [ERROR] No WebGPT2 URL entered. Live test not started.
  exit /b 2
)
set "SMARTAGENT_GAR_A0_DIAGNOSTIC=1"
set "SMARTAGENT_GAR_V1=1"
rem Legacy D1 replay gate requires original takeover semantics.
set "SMARTAGENT_GAR_L4_SUPERVISOR_V1=0"
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=1"
echo.
echo [GAR D1] Effective flags: GAR=%SMARTAGENT_GAR_V1% D1=%SMARTAGENT_GAR_D1_FAULT_INJECTION_V1%
echo [GAR D1] Startup diagnostic event: gar_d1_test_configuration
echo [GAR D1] The exact one-shot test task is: Explain RAII in C++.
echo.
call "%~dp0launch_webcopilot.bat"
set "RC=%ERRORLEVEL%"
echo [GAR D1] Controller exited: %RC%
exit /b %RC%
