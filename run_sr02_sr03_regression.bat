@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 (
  echo [SR-GATE][ERROR] Cannot enter repository root.
  exit /b 2
)
echo [SR-GATE] WebGPT1 Readback + V9 Format Regression (OFFLINE)
if not exist "source\WebAgent\tests\validate_shared_runtime_sr02_sr03.py" (
  echo [SR-GATE][ERROR] Missing SR-02/SR-03 test module.
  exit /b 2
)
if not exist "config\protocol_manifest.json" (
  echo [SR-GATE][ERROR] Strict manifest missing.
  exit /b 2
)
set "PY_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PY_EXE%" (
  echo [SR-GATE][ERROR] Project virtualenv Python missing: "%PY_EXE%"
  exit /b 2
)
"%PY_EXE%" -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] Project Python is not 3.10+.
  exit /b 2
)
set "PYTHONPATH=%CD%\source"
set "PYTHONSAFEPATH=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "SMARTAGENT_GAR_V1=0"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL="
set "TRI_ONE_TASK_RESULT_V1=1"
set "TRI_ONE_TEXT_ONLY_V1=1"
where git >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] git.exe not found in CMD PATH.
  exit /b 2
)
git diff --quiet -- config/protocol_manifest.json source/agent_core/protocol_v8.py source/agent_core/protocol_v9.py source/agent_core/narrative_bridge.py source/agent_core/web_runtime.py source/WebAgent/tests/validate_shared_runtime_sr02_sr03.py source/WebAgent/tests/validate_narrative_v9_bridge.py source/WebAgent/tests/validate_protocol_transport_normalization.py
if errorlevel 1 (
  echo [SR-GATE][ERROR] Tracked parser or test source has unstaged edits.
  git status --short
  exit /b 2
)
git diff --cached --quiet -- config/protocol_manifest.json source/agent_core/protocol_v8.py source/agent_core/protocol_v9.py source/agent_core/narrative_bridge.py source/agent_core/web_runtime.py source/WebAgent/tests/validate_shared_runtime_sr02_sr03.py source/WebAgent/tests/validate_narrative_v9_bridge.py source/WebAgent/tests/validate_protocol_transport_normalization.py
if errorlevel 1 (
  echo [SR-GATE][ERROR] Tracked parser or test source has staged edits.
  git status --short
  exit /b 2
)
set "STAMP="
for /f "delims=" %%I in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd_HHmmss 2^>nul') do set "STAMP=%%I"
if not defined STAMP set "STAMP=run"
set "LOGDIR=%CD%\localdata\logs\shared-runtime-sr02-sr03\%STAMP%_%RANDOM%%RANDOM%"
mkdir "%LOGDIR%" >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] Cannot create: "%LOGDIR%"
  exit /b 2
)
set "HEAD_SHA="
for /f "delims=" %%I in ('git rev-parse HEAD 2^>nul') do set "HEAD_SHA=%%I"
if not defined HEAD_SHA (
  echo [SR-GATE][ERROR] Unable to resolve Git HEAD.
  exit /b 2
)
(
  echo Shared Runtime SR-02 SR-03
  echo sha=%HEAD_SHA%
  echo started=%DATE% %TIME%
) > "%LOGDIR%\summary.txt"
echo [SR-GATE] Git HEAD: %HEAD_SHA%
echo [SR-GATE] Logs: "%LOGDIR%"
echo.
set /a TOTAL=0
set /a PASSED=0
set /a FAILED=0
call :TEST validate_shared_runtime_sr02_sr03
call :TEST validate_protocol_transport_normalization
call :TEST validate_narrative_v9_bridge
call :TEST validate_single_fence_transport_contract
call :TEST validate_protocol_recovery_freshness
call :TEST validate_protocol_recovery_readback
call :TEST validate_tri_one_5a_protocol_it
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
if %FAILED% GTR 0 (
  echo ============================================================
  echo [SR-GATE] FAILURE DETAILS - LAST 80 LINES FOR SCREENSHOT
  echo ============================================================
  for /f "tokens=2" %%T in ('findstr /B /C:"FAIL " "%LOGDIR%\summary.txt"') do call :SHOW_FAILURE "%%T"
  echo [SR-GATE][FAIL] Do not start Live.
  exit /b 1
)
echo [SR-GATE][PASS] OFFLINE 8/8. Live SR-02 and SR-03 NOT YET verified.
exit /b 0

:TEST
set /a TOTAL+=1
set "NAME=%~1"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/8] %NAME%
"%PY_EXE%" -B "source\WebAgent\tests\%NAME%.py" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo [FAIL] %NAME% exit=%RC%
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
)
exit /b 0

:MANIFEST
set /a TOTAL+=1
set "NAME=strict_manifest"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/8] %NAME%
"%PY_EXE%" -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); print(r); assert r['core_file_count']==203 and r['hash_verification']=='PASS' and not r['hash_mismatches'],r" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  echo [FAIL] %NAME% exit=%RC%
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
)
exit /b 0

:SHOW_FAILURE
set "BAD_TEST=%~1"
set "BAD_LOG=%LOGDIR%\%BAD_TEST%.log"
echo.
echo [FAILED CASE] %BAD_TEST%
if exist "%BAD_LOG%" (
  "%PY_EXE%" -c "import pathlib,sys; print('\n'.join(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8',errors='replace').splitlines()[-80:]))" "%BAD_LOG%"
) else (
  echo [ERROR] Missing detail file "%BAD_LOG%"
)
echo [FAILED LOG] "%BAD_LOG%"
exit /b 0
