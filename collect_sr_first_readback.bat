@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [SR-BUNDLE][ERROR] SmartAgent project Python missing.
  exit /b 2
)
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
"%PY%" -B source\WebAgent\tests\collect_sr_readback_bundle.py "%CD%"
if errorlevel 1 (
  echo [SR-BUNDLE][ERROR] ZIP not created. Check first-readback/regression evidence.
  exit /b 1
)
echo [SR-BUNDLE] Inspect ZIP contents before uploading.
exit /b 0
