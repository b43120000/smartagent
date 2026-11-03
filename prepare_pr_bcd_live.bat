@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [PR-BCD] Bundled Python not found. Use the configured SmartAgent venv.
  exit /b 2
)
".venv\Scripts\python.exe" -B "source\WebAgent\tests\prepare_pr_bcd_live_fixture.py" --root "E:\SmartAgent_PR_BCD_LIVE"
if errorlevel 1 (
  echo [PR-BCD] Fixture preparation failed. No Live task was started.
  exit /b 1
)
echo [PR-BCD] Synthetic fixture ready. Nothing was installed.
exit /b 0
