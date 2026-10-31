@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "REQUIRED_BASE=dc1ee74e77620a2ed05b8d05d9557be269006b1d"
where git >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] git executable missing from CMD PATH.
  exit /b 2
)
git merge-base --is-ancestor %REQUIRED_BASE% HEAD >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] HEAD is not based on the approved 75febba baseline.
  exit /b 2
)
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [SR-GATE][ERROR] SmartAgent Python not found in .venv.
  exit /b 2
)
"%PY%" -c "import sys;assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 (
  echo [SR-GATE][ERROR] Python 3.10+ required.
  exit /b 2
)
if not exist "source\agent_core\sr_first_readback.py" (
  echo [SR-GATE][ERROR] First readback module missing.
  exit /b 2
)
git diff --quiet -- config/protocol_manifest.json source/agent_core/web_runtime.py source/WebAgent/controller.py source/agent_core/sr_first_readback.py source/WebAgent/tests/validate_sr_first_readback_gate.py source/WebAgent/tests/collect_sr_readback_bundle.py
if errorlevel 1 (
  echo [SR-GATE][ERROR] SR source has unstaged edits.
  git status --short
  exit /b 2
)
git diff --cached --quiet -- config/protocol_manifest.json source/agent_core/web_runtime.py source/WebAgent/controller.py source/agent_core/sr_first_readback.py source/WebAgent/tests/validate_sr_first_readback_gate.py source/WebAgent/tests/collect_sr_readback_bundle.py
if errorlevel 1 (
  echo [SR-GATE][ERROR] SR source has staged edits.
  git status --short
  exit /b 2
)
set "PYTHONPATH=%CD%\source"
set "PYTHONSAFEPATH=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=0"
set "SMARTAGENT_GAR_V1=0"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "SMARTAGENT_GAR_FRESH_CONTEXT_URL="
set "STAMP="
for /f "delims=" %%I in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd_HHmmss 2^>nul') do set "STAMP=%%I"
if not defined STAMP set "STAMP=run"
set "LOGDIR=%CD%\localdata\logs\sr-first-readback-regression\%STAMP%_%RANDOM%%RANDOM%"
mkdir "%LOGDIR%" >nul 2>&1
if errorlevel 1 exit /b 2
for /f "delims=" %%I in ('git rev-parse HEAD') do set "SHA=%%I"
(
  echo SR First Readback Offline Regression
  echo sha=%SHA%
  echo started=%DATE% %TIME%
) > "%LOGDIR%\summary.txt"
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
call :TEST validate_sr_first_readback_gate
call :TEST validate_sr04_sr05_evidence
call :MANIFEST
(
  echo commands=%TOTAL%
  echo passed=%PASSED%
  echo failed=%FAILED%
  echo completed=%DATE% %TIME%
) >> "%LOGDIR%\summary.txt"
echo.
type "%LOGDIR%\summary.txt"
echo Logs: "%LOGDIR%"
if %FAILED% GTR 0 (
  echo ============================================================
  echo SR FAILURE DETAILS - FOR SCREENSHOT
  echo ============================================================
  for /f "tokens=2" %%T in ('findstr /B /C:"FAIL " "%LOGDIR%\summary.txt"') do call :SHOW_FAILURE "%%T"
  echo [SR-GATE][FAIL] Do not run Live.
  exit /b 1
)
echo [SR-GATE][PASS] OFFLINE ONLY; SR-04 / SR-05 Live remain unverified.
exit /b 0

:TEST
set /a TOTAL+=1
set "NAME=%~1"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/10] %NAME%
"%PY%" -B "source\WebAgent\tests\%NAME%.py" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
  echo [FAIL] %NAME% exit=%RC%
)
exit /b 0

:MANIFEST
set /a TOTAL+=1
set "NAME=strict_manifest"
set "LOG=%LOGDIR%\%NAME%.log"
echo [%TOTAL%/10] %NAME%
"%PY%" -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); print(r); assert r['core_file_count']==203 and r['hash_verification']=='PASS' and not r['hash_mismatches'],r" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  set /a PASSED+=1
  >> "%LOGDIR%\summary.txt" echo PASS %NAME% exit=0
) else (
  set /a FAILED+=1
  >> "%LOGDIR%\summary.txt" echo FAIL %NAME% exit=%RC%
  echo [FAIL] %NAME% exit=%RC%
)
exit /b 0

:SHOW_FAILURE
set "NAME=%~1"
set "FILE=%LOGDIR%\%NAME%.log"
echo.
echo [FAILED CASE] %NAME%
if exist "%FILE%" (
  "%PY%" -c "import pathlib,sys; print('\n'.join(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8',errors='replace').splitlines()[-80:]))" "%FILE%"
) else (
  echo [ERROR] Missing test log: "%FILE%"
)
exit /b 0
