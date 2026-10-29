@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem TEMPORARY GAR A0 regression runner. Remove before merging to main.
rem Does not change Git/Source/Manifest, or start/stop Controller.

set "REQUIRED_FIX_SHA=f4be45552e1e1916ff12916911f94b41f437382c"
cd /d "%~dp0" || goto :FATAL
if not exist "source\WebAgent\tests\validate_gar_a0_identity.py" (
  echo [ERROR] Missing GAR A0 test source. Checkout the updated GAR branch.
  goto :FATAL
)
if not exist "config\protocol_manifest.json" goto :FATAL

set "CURRENT_SHA="
for /f "delims=" %%I in ('git rev-parse HEAD 2^>nul') do set "CURRENT_SHA=%%I"
if not defined CURRENT_SHA goto :FATAL
git merge-base --is-ancestor %REQUIRED_FIX_SHA% HEAD >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Current Git HEAD is not the GAR A0 source-fix commit or its descendant.
  echo   Required ancestor: %REQUIRED_FIX_SHA%
  echo   Actual HEAD:       %CURRENT_SHA%
  goto :FATAL
)
git diff --quiet -- config/protocol_manifest.json source/WebAgent/protocol_loop.py source/WebAgent/controller.py source/agent_core/source_attestation.py source/WebAgent/tests/validate_gar_a0_identity.py
if errorlevel 1 (
  echo [ERROR] Modified GAR Source, test or Manifest; tests not started.
  goto :FATAL
)
git diff --cached --quiet -- config/protocol_manifest.json source/WebAgent/protocol_loop.py source/WebAgent/controller.py source/agent_core/source_attestation.py source/WebAgent/tests/validate_gar_a0_identity.py
if errorlevel 1 (
  echo [ERROR] Staged GAR Source, test or Manifest; tests not started.
  goto :FATAL
)

set "PY_CMD=python"
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 (
  set "PY_CMD=py -3"
  py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer was not found.
    goto :FATAL
  )
)
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
set "STAMP="
for /f "delims=" %%I in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd_HHmmss 2^>nul') do set "STAMP=%%I"
if not defined STAMP set "STAMP=unknown"
set "LOGDIR=%CD%\localdata\logs\gar-a0-regression\%STAMP%_%RANDOM%%RANDOM%"
mkdir "%LOGDIR%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Unable to create log folder.
  goto :FATAL
)
(
  echo GAR A0 offline regression
  echo required_source_fix=%REQUIRED_FIX_SHA%
  echo actual_sha=%CURRENT_SHA%
  echo python_command=%PY_CMD%
  echo started=%DATE% %TIME%
) > "%LOGDIR%\summary.txt"
%PY_CMD% --version > "%LOGDIR%\python-version.txt" 2>&1
git status --short > "%LOGDIR%\git-status.txt" 2>&1

echo GAR Regression - current SHA: %CURRENT_SHA%
echo Logs: %LOGDIR%
set /a TOTAL=0
set /a PASSED=0
set /a FAILED=0
call :RUN_TEST validate_gar_a0_identity
call :RUN_TEST validate_gar_shared_runtime
call :RUN_TEST validate_gar_protocol_loop_it
call :RUN_TEST validate_gar_l4_fresh_context
call :RUN_TEST validate_tri_one_5a_protocol_it
call :RUN_TEST validate_gar_terminal_empty_evidence_boundary
call :RUN_MANIFEST

(
 echo total_commands=%TOTAL%
 echo passed_commands=%PASSED%
 echo failed_commands=%FAILED%
 echo completed=%DATE% %TIME%
) >> "%LOGDIR%\summary.txt"
echo.
type "%LOGDIR%\summary.txt"
echo Logs: "%LOGDIR%"
if %FAILED% GTR 0 (
  echo [FAIL] Preserve all logs. Do not proceed to GAR Live.
  pause
  exit /b 1
)
echo [PASS] Offline only; this does NOT prove any running Controller identity.
pause
exit /b 0

:RUN_TEST
set /a TOTAL+=1
set "NAME=%~1"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/7] Running %NAME% ...
%PY_CMD% -B "source\WebAgent\tests\%NAME%.py" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  echo PASS %NAME%
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo FAIL %NAME% exit=%RC%
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
  type "%LOG%"
)
exit /b 0

:RUN_MANIFEST
set /a TOTAL+=1
set "NAME=strict_manifest"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/7] Running %NAME% ...
%PY_CMD% -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); print(r); assert r['core_file_count']==203 and r['hash_verification']=='PASS' and not r['hash_mismatches'],r" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  echo PASS %NAME%
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo FAIL %NAME% exit=%RC%
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
  type "%LOG%"
)
exit /b 0

:FATAL
echo [ERROR] Preflight failed; no regression was started.
pause
exit /b 2
