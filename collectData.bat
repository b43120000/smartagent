@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
rem Click after the GAR D1 Live task succeeds or safely pauses.
rem Read-only evidence collection: does not stop/restart Controller or browser.
cd /d "%~dp0"
if errorlevel 1 (
  echo [ERROR] Unable to enter the SmartAgent root.
  pause
  exit /b 2
)
set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PY_EXE%" (
  echo [ERROR] Missing project virtualenv: "%PY_EXE%"
  echo [HINT] Install the standard WebCopilot environment first.
  pause
  exit /b 2
)
if not exist "source\WebAgent\tools\collect_gar_l4_debug.py" (
  echo [ERROR] Missing GAR L4 evidence collector.
  pause
  exit /b 2
)
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
echo ============================================================
echo GAR L4 data collection - no Controller or browser modification
echo ============================================================
"%PY_EXE%" -B "source\WebAgent\tools\collect_gar_l4_debug.py" --root "%CD%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo [SUCCESS] Data collection completed.
  echo [NEXT] Upload the ZIP printed above, or ZIP the displayed folder.
  echo [PRIVACY] Inspect webgpt2_review_redacted.json before sharing.
) else (
  echo [FAILED] Data collection failed. Error code: %RC%.
)
echo.
pause
exit /b %RC%
