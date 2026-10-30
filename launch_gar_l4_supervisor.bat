@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
rem GAR L4: WebGPT1 executes; WebGPT2 provides one read-only recovery review.
rem Fill the WebGPT2 URL. WebGPT1 URL may stay blank (saved WebCopilot setting).
rem Use the configured existing Chrome3 session; never create/close a profile.
set "GAR_WEBGPT1_URL="
set "GAR_WEBGPT2_URL="
rem Example: set "GAR_WEBGPT2_URL=https://chatgpt.com/g/.../c/..."
cd /d "%~dp0"
if errorlevel 1 exit /b 2
if not exist "%~dp0launch_webcopilot.bat" (
  echo [GAR L4] Missing launch_webcopilot.bat
  exit /b 2
)
if not defined GAR_WEBGPT2_URL (
  set /p "GAR_WEBGPT2_URL=WebGPT2 existing conversation URL: "
)
if not defined GAR_WEBGPT2_URL (
  echo [GAR L4] WebGPT2 URL required. Controller not started.
  exit /b 2
)
set "SMARTAGENT_GAR_V1=1"
set "SMARTAGENT_GAR_L4_SUPERVISOR_V1=1"
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL=%GAR_WEBGPT2_URL%"
set "SMARTAGENT_GAR_A0_DIAGNOSTIC=1"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
echo.
echo [GAR L4] Supervisor ON; D1 fault injection OFF.
echo [GAR L4] Original WebGPT1 retains task/tool execution authority.
echo [GAR L4] WebGPT2 tab will be opened/reused at controller startup.
if defined GAR_WEBGPT1_URL (
  call "%~dp0launch_webcopilot.bat" --webgpt-url "%GAR_WEBGPT1_URL%"
) else (
  call "%~dp0launch_webcopilot.bat"
)
set "RC=%ERRORLEVEL%"
echo [GAR L4] Controller exited with code %RC%.
exit /b %RC%
