@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem GAR D1 WebCopilot injection: offline-only regression, no browser/Telegram.
rem Uses the SAME project .venv Python as launch_webcopilot.bat.
chcp 65001 >nul 2>&1
rem Stable ancestor gate rather than impossible self-referential commit SHA.
set "REQUIRED_BASE=51072718ebfe11a8e460d51134ecbc01e6bfa900"
cd /d "%~dp0"
if errorlevel 1 (
  echo [ERROR] Cannot enter BAT directory: %~dp0
  goto :FATAL
)
echo [D1 Preflight] Repo: %CD%
if not exist "source\WebAgent\tests\validate_gar_d1_fault_injection.py" (
  echo [ERROR] D1 test file missing. Switch to correct source commit.
  goto :FATAL
)
if not exist "config\protocol_manifest.json" (
  echo [ERROR] Strict manifest missing.
  goto :FATAL
)
where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] git.exe missing from Windows CMD PATH. Run through Git Bash or add Git\cmd to PATH.
  goto :FATAL
)
set "HEAD_SHA="
for /f "delims=" %%I in ('git rev-parse HEAD 2^>nul') do set "HEAD_SHA=%%I"
if not defined HEAD_SHA (
  echo [ERROR] git rev-parse HEAD failed from CMD.
  goto :FATAL
)
echo [D1 Preflight] Git HEAD: %HEAD_SHA%
git merge-base --is-ancestor %REQUIRED_BASE% HEAD >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Revision is not descended from C1 baseline %REQUIRED_BASE%.
  echo [ERROR] Actual HEAD: %HEAD_SHA%
  goto :FATAL
)
rem Preserve existing local Manifest changes; do not stash, reset or overwrite.
git diff --quiet -- config/protocol_manifest.json source/WebAgent/controller.py source/WebAgent/fresh_context.py source/WebAgent/tests/validate_gar_d1_fault_injection.py source/WebAgent/tests/validate_gar_l4_fresh_context.py source/agent_core/web_runtime.py source/WebAgent/tests/validate_protocol_recovery_freshness.py run_gar_d1_regression.bat source/agent_core/narrative_bridge.py source/WebAgent/tests/validate_narrative_v9_bridge.py source/agent_core/goal_anchored_recovery.py source/WebAgent/protocol_loop.py source/WebAgent/tests/validate_gar_shared_runtime.py source/WebAgent/tests/validate_gar_protocol_loop_it.py
if errorlevel 1 (
  echo [ERROR] Tracked GAR D1 source, test, or manifest has unstaged edits.
  git status --short
  goto :FATAL
)
git diff --cached --quiet -- config/protocol_manifest.json source/WebAgent/controller.py source/WebAgent/fresh_context.py source/WebAgent/tests/validate_gar_d1_fault_injection.py source/WebAgent/tests/validate_gar_l4_fresh_context.py source/agent_core/web_runtime.py source/WebAgent/tests/validate_protocol_recovery_freshness.py run_gar_d1_regression.bat source/agent_core/narrative_bridge.py source/WebAgent/tests/validate_narrative_v9_bridge.py source/agent_core/goal_anchored_recovery.py source/WebAgent/protocol_loop.py source/WebAgent/tests/validate_gar_shared_runtime.py source/WebAgent/tests/validate_gar_protocol_loop_it.py
if errorlevel 1 (
  echo [ERROR] Tracked GAR D1 source, test, or manifest has staged edits.
  git status --short
  goto :FATAL
)
rem Match launch_webcopilot.bat first; do not depend on global Python PATH.
set "PY_EXE="
set "PY_ARGS="
if exist "%CD%\.venv\Scripts\python.exe" (
  set "PY_EXE=%CD%\.venv\Scripts\python.exe"
) else (
  python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
  if not errorlevel 1 set "PY_EXE=python"
  if not defined PY_EXE (
    py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
    if not errorlevel 1 (
      set "PY_EXE=py"
      set "PY_ARGS=-3"
    )
  )
)
if not defined PY_EXE (
  echo [ERROR] No usable Python 3.10+ found.
  echo [HINT] WebCopilot normally uses .venv\Scripts\python.exe.
  echo [HINT] Check the installed virtualenv in this SAME checkout.
  goto :FATAL
)
"%PY_EXE%" %PY_ARGS% -c "import sys; print(sys.executable); assert sys.version_info >= (3,10)"
if errorlevel 1 (
  echo [ERROR] Selected project Python is not usable or is older than 3.10.
  goto :FATAL
)
echo [D1 Preflight] Python: "%PY_EXE%" %PY_ARGS%
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "STAMP="
for /f "delims=" %%I in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd_HHmmss 2^>nul') do set "STAMP=%%I"
if not defined STAMP set "STAMP=run"
set "LOGDIR=%CD%\localdata\logs\gar-d1-regression\%STAMP%_%RANDOM%%RANDOM%"
mkdir "%LOGDIR%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot create log directory: %LOGDIR%
  goto :FATAL
)
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
call :TEST validate_protocol_recovery_freshness
call :TEST validate_narrative_v9_bridge
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
  echo.
  echo ============================================================
  echo [GAR D1] FAILED TEST DETAILS -- PRINTED AT END FOR SCREENSHOTS
  echo ============================================================
  for /f "tokens=2" %%T in ('findstr /B /C:"FAIL " "%LOGDIR%\summary.txt"') do call :SHOW_FAILURE "%%T"
  echo ============================================================
  echo [FAIL] Offline gate failed. Do not do Live.
  echo [FAIL] Local evidence: "%LOGDIR%"
  exit /b 1
)
echo [PASS] Offline only. Chrome3 L4 Live remains a separate acceptance gate.
exit /b 0

:TEST
set /a TOTAL+=1
set "NAME=%~1"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/14] %NAME%
"%PY_EXE%" %PY_ARGS% -B "source\WebAgent\tests\%NAME%.py" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo [FAIL] %NAME% exit=%RC% -- traceback printed at END of the run
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
)
exit /b 0

:MANIFEST
set /a TOTAL+=1
set "NAME=strict_manifest"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/14] %NAME%
"%PY_EXE%" %PY_ARGS% -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); print(r); assert r['core_file_count']==204 and r['hash_verification']=='PASS' and not r['hash_mismatches'],r" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo [FAIL] %NAME% exit=%RC% -- traceback printed at END of the run
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
)
exit /b 0

:SHOW_FAILURE
set "BAD_TEST=%~1"
set "BAD_LOG=%LOGDIR%\%BAD_TEST%.log"
echo.
echo [FAILED CASE] %BAD_TEST%
if not exist "%BAD_LOG%" (
  echo [ERROR] Missing detail log: "%BAD_LOG%"
  exit /b 0
)
echo [TRACEBACK] Last 80 lines of the failed test:
"%PY_EXE%" %PY_ARGS% -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); print('\n'.join(p.read_text(encoding='utf-8',errors='replace').splitlines()[-80:]))" "%BAD_LOG%"
if errorlevel 1 (
  echo [ERROR] Could not display test log with Python; printing it in full:
  type "%BAD_LOG%"
)
echo [FAILED LOG] "%BAD_LOG%"
exit /b 0

:FATAL
echo [ERROR] D1 preflight failed. No regressions run.
exit /b 2
