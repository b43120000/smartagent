@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
rem Supplemental D1 v2/L4/collector offline gate. Existing GAR regression BATs untouched.
cd /d "%~dp0"
if errorlevel 1 goto :FATAL
set "PY_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PY_EXE%" (
  echo [ERROR] Project Python virtualenv missing.
  goto :FATAL
)
set "PYTHONPATH=%CD%\source"
set "PYTHONDONTWRITEBYTECODE=1"
set "SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0"
set "LOGDIR=%CD%\localdata\logs\gar-debug-regression"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if errorlevel 1 goto :FATAL
set "RUN_LOG=%LOGDIR%\latest.txt"
echo GAR D1 v2 + GAR L4 supervisor + Collector offline regression > "%RUN_LOG%"
git rev-parse HEAD >> "%RUN_LOG%" 2>&1
set /a FAILED=0
set /a TOTAL=0
echo ============================================================
echo GAR debug supplemental offline regressions
echo ============================================================
call :TEST validate_gar_l4_supervision
call :TEST validate_gar_d1_fault_injection
call :TEST validate_gar_debug_collector
set /a TOTAL+=1
echo [4/4] strict manifest
"%PY_EXE%" -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); assert r['core_file_count']==204 and r['hash_verification']=='PASS' and not r['hash_mismatches'],r; print('PASS strict_manifest')" >> "%RUN_LOG%" 2>&1
if errorlevel 1 (
  echo [FAIL] strict manifest
  set /a FAILED+=1
) else (
  echo [PASS] strict manifest
)
echo.
if %FAILED% GTR 0 (
  echo [FAIL] Failed commands: %FAILED% / 4
  echo [LOG] "%RUN_LOG%"
  type "%RUN_LOG%"
  echo [BLOCKED] Do not claim Live PASS.
  pause
  exit /b 1
)
echo [PASS] All four offline checks.
echo [LOG] "%RUN_LOG%"
echo [INFO] Live GAR verification is separate. Run GAR_debug_webcopiolt.bat.
pause
exit /b 0

:TEST
set /a TOTAL+=1
echo [%TOTAL%/4] %~1
"%PY_EXE%" -B "source\WebAgent\tests\%~1.py" >> "%RUN_LOG%" 2>&1
if errorlevel 1 (
  echo [FAIL] %~1
  set /a FAILED+=1
) else (
  echo [PASS] %~1
)
exit /b 0

:FATAL
echo [ERROR] GAR debug regression preflight failed.
pause
exit /b 2
