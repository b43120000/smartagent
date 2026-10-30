@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem GAR D1 WebCopilot injection: offline-only regression, no browser/Telegram.
rem Stable ancestor gate rather than impossible self-referential commit SHA.
set "REQUIRED_BASE=51072718ebfe11a8e460d51134ecbc01e6bfa900"
cd /d "%~dp0" || goto :FATAL
if not exist "source\WebAgent\tests\validate_gar_d1_fault_injection.py" goto :FATAL
if not exist "config\protocol_manifest.json" goto :FATAL
set "HEAD_SHA="
for /f "delims=" %%I in ('git rev-parse HEAD 2^>nul') do set "HEAD_SHA=%%I"
if not defined HEAD_SHA goto :FATAL
git merge-base --is-ancestor %REQUIRED_BASE% HEAD >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Revision is not descended from C1 baseline %REQUIRED_BASE%.
  goto :FATAL
)
rem Preserve existing local Manifest changes; do not stash, reset or overwrite.
git diff --quiet -- config/protocol_manifest.json source/WebAgent/controller.py source/WebAgent/tests/validate_gar_d1_fault_injection.py
if errorlevel 1 (
  echo [ERROR] Tracked GAR D1 source, test, or manifest has unstaged edits.
  goto :FATAL
)
git diff --cached --quiet -- config/protocol_manifest.json source/WebAgent/controller.py source/WebAgent/tests/validate_gar_d1_fault_injection.py
if errorlevel 1 (
  echo [ERROR] Tracked GAR D1 source, test, or manifest has staged edits.
  goto :FATAL
)
set "PY_CMD=python"
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 (
  set "PY_CMD=py -3"
  py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
  if errorlevel 1 goto :FATAL
)
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "STAMP="
for /f "delims=" %%I in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd_HHmmss 2^>nul') do set "STAMP=%%I"
if not defined STAMP set "STAMP=run"
set "LOGDIR=%CD%\localdata\logs\gar-d1-regression\%STAMP%_%RANDOM%%RANDOM%"
mkdir "%LOGDIR%" >nul 2>&1
if errorlevel 1 goto :FATAL
(
  echo GAR D1 offline regression
  echo sha=%HEAD_SHA%
  echo started=%DATE% %TIME%
) > "%LOGDIR%\summary.txt"
git status --short > "%LOGDIR%\git-status.txt" 2>&1
echo.
echo [GAR D1] Checkout: %HEAD_SHA%
echo [GAR D1] Offline evidence: %LOGDIR%
echo.
set /a TOTAL=0
set /a PASSED=0
set /a FAILED=0
call :TEST validate_gar_a0_identity
call :TEST validate_browser_startup_settle
call :TEST validate_composer_stability
call :TEST validate_composer_recovery
call :TEST validate_composer_operation
call :TEST validate_gar_shared_runtime
call :TEST validate_gar_protocol_loop_it
call :TEST validate_gar_l4_fresh_context
call :TEST validate_tri_one_5a_protocol_it
call :TEST validate_gar_terminal_empty_evidence_boundary
call :TEST validate_gar_d1_fault_injection
call :MANIFEST
(
  echo commands=%TOTAL%
  echo passed=%PASSED%
  echo failed=%FAILED%
  echo completed=%DATE% %TIME%
) >> "%LOGDIR%\summary.txt"
echo.
type "%LOGDIR%\summary.txt"
echo.
echo Logs: "%LOGDIR%"
if %FAILED% GTR 0 (
  echo [FAIL] Offline gate failed. Do not do Live.
  exit /b 1
)
echo [PASS] Offline only. Chrome3 L4 Live remains a separate acceptance gate.
exit /b 0

:TEST
set /a TOTAL+=1
set "NAME=%~1"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/12] %NAME%
%PY_CMD% -B "source\WebAgent\tests\%NAME%.py" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo [FAIL] %NAME% exit=%RC%
  type "%LOG%"
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
)
exit /b 0

:MANIFEST
set /a TOTAL+=1
set "NAME=strict_manifest"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/12] %NAME%
%PY_CMD% -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); print(r); assert r['core_file_count']==203 and r['hash_verification']=='PASS' and not r['hash_mismatches'],r" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo [FAIL] %NAME% exit=%RC%
  type "%LOG%"
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
)
exit /b 0

:FATAL
echo [ERROR] D1 preflight failed. No regressions run.
exit /b 2
