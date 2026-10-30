@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
rem GAR D1 v2 live E2E: exact safe request, WebGPT2 read-only review, original WebGPT1 resumes.
rem Uses the configured original WebCopilot/Chrome3 profile and existing WebGPT1 startup preferences.
rem Does not modify Source, Manifest, Git, or launch a second Controller.
cd /d "%~dp0"
if errorlevel 1 (
  echo [ERROR] Cannot enter SmartAgent root.
  pause
  exit /b 2
)
if not exist "%~dp0launch_webcopilot.bat" (
  echo [ERROR] Missing launch_webcopilot.bat
  pause
  exit /b 2
)
set "GAR_WEBGPT2_URL=https://chatgpt.com/g/g-p-6a818bab5be0819188aa5bfab333f9aa-localagentspace/c/6ae48727-ac34-83ee-9c89-c320e0423a67"
rem Optional: pass --webgpt-url for WebGPT1 on the command line; otherwise use saved startup settings.
set "SMARTAGENT_GAR_V1=1"
set "SMARTAGENT_GAR_L4_SUPERVISOR_V1=1"
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL=%GAR_WEBGPT2_URL%"
set "SMARTAGENT_GAR_A0_DIAGNOSTIC=1"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=1"
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
echo ============================================================
echo GAR D1 v2 DEBUG - WebGPT1 + WebGPT2 supervisor
echo ============================================================
echo [IMPORTANT] Close the existing Controller normally before starting.
echo [IMPORTANT] Keep Chrome3 browser and both conversations open.
echo [CONFIG] GAR=1; L4 supervisor=1; D1 single-use fault injection=1
echo [CONFIG] WebGPT1 uses saved conversation unless overridden.
echo [CONFIG] WebGPT2 is an existing conversation; no new profile.
echo.
echo After READY, send EXACTLY this one line in WebGPT1:
echo.
echo   Explain RAII in C++.
echo.
echo D1 accepts only that exact request without files; one test per Controller start.
echo When the task has finished or safely paused, run collectData.bat.
echo Do not start another Controller while this window is active.
echo ============================================================
call "%~dp0launch_webcopilot.bat" %*
set "RC=%ERRORLEVEL%"
echo.
echo [GAR DEBUG] Controller exited, code=%RC%.
echo [GAR DEBUG] Evidence collection: double-click collectData.bat.
pause
exit /b %RC%
