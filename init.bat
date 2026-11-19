@echo off
rem Git67 IT10 diagnostic CMD initialization. Deliberately NO setlocal:
rem variables persist in this SAME CMD for the RemoteAgent and collect.bat.
if /I "%~1"=="--launcher-self-test" (
    echo GIT67_INIT_BAT_PARSE_OK
    exit /b 0
)
cd /d "%~dp0"
set "SMARTAGENT_IT10_AUTO_PUBLISH=OFF"
set "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED="
set "SMARTAGENT_IT10_PATH_TRACE="
set "SMARTAGENT_IT10_PATH_TRACE_RAW="
set "SMARTAGENT_IT10_PATH_TRACE_RUN_ID="
set "SMARTAGENT_IT10_PATH_TRACE_ROOT="
set "SMARTAGENT_SOURCE=%~dp0source"
set "PYTHONPATH=%~dp0source"
set "PYTHONSAFEPATH=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [INIT] FAIL: the repo .venv Python interpreter is missing.
    goto :init_failed
)
set "GIT67_BRANCH="
for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "GIT67_BRANCH=%%B"
if /I not "%GIT67_BRANCH%"=="git67/case-c-action-contract-repair" (
    echo [INIT] FAIL: switch to git67/case-c-action-contract-repair first.
    goto :init_failed
)
set "GIT67_DIRTY="
for /f "delims=" %%S in ('git status --porcelain 2^>nul') do set "GIT67_DIRTY=YES"
if defined GIT67_DIRTY (
    echo [INIT] FAIL: tracked or untracked working tree changes. Review git status first.
    goto :init_failed
)
"%~dp0.venv\Scripts\python.exe" -B -c "from agent_core.protocol_manifest import require_protocol_manifest; r=require_protocol_manifest('.'); assert r.get('hash_verification') == 'PASS' and r.get('hash_mismatches') == [] and r.get('status') == 'PASS'; print('GIT67_MANIFEST_VERIFIED=YES')"
if errorlevel 1 (
    echo [INIT] FAIL: Protocol Manifest is not byte verified.
    goto :init_failed
)
where gh >nul 2>nul
if errorlevel 1 (
    echo [INIT] FAIL: GitHub CLI gh is missing; install or configure gh first.
    goto :init_failed
)
set "GIT67_TEST_PRIVATE="
for /f "delims=" %%P in ('gh api repos/b43120000/test --jq ".private" 2^>nul') do set "GIT67_TEST_PRIVATE=%%P"
if /I not "%GIT67_TEST_PRIVATE%"=="true" (
    echo [INIT] FAIL: Git Test repo must be accessible and verified PRIVATE.
    goto :init_failed
)
set "GIT67_TEST_PUSH="
for /f "delims=" %%P in ('gh api repos/b43120000/test --jq ".permissions.push" 2^>nul') do set "GIT67_TEST_PUSH=%%P"
if /I not "%GIT67_TEST_PUSH%"=="true" (
    echo [INIT] FAIL: GitHub CLI does not have Git Test push access.
    goto :init_failed
)
set "GIT67_SESSION_UUID="
for /f "delims=" %%R in ('powershell.exe -NoProfile -Command "[guid]::NewGuid().ToString('N')"') do set "GIT67_SESSION_UUID=%%R"
if not defined GIT67_SESSION_UUID (
    echo [INIT] FAIL: could not allocate a unique run ID.
    goto :init_failed
)
set "GIT67_SESSION_RUN_ID=GIT67_%GIT67_SESSION_UUID%"
set "GIT67_SESSION_RAW_ROOT=%~dp0localdata\diagnostics\it10_GIT67_%GIT67_SESSION_UUID%"
rem Avoid a quoted trailing backslash on -ProjectRoot: Windows native argv
rem parsing can swallow the closing quote and the following -PrivateRoot.
rem -NonInteractive fails instead of prompting for any missing mandatory arg.
if not defined GIT67_SESSION_RAW_ROOT (
    echo [INIT] FAIL: private Raw Evidence path was not generated.
    goto :init_failed
)
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0install_smart_agent\git67_it10_prepare.ps1" -ProjectRoot "%~dp0." -PrivateRoot "%GIT67_SESSION_RAW_ROOT%"
if errorlevel 1 (
    echo [INIT] FAIL: private evidence folder ACL could not be verified.
    goto :init_failed
)
set "SMARTAGENT_IT10_PATH_TRACE=1"
set "SMARTAGENT_IT10_PATH_TRACE_RAW=YES"
set "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED=YES"
set "SMARTAGENT_IT10_PATH_TRACE_RUN_ID=%GIT67_SESSION_RUN_ID%"
set "SMARTAGENT_IT10_PATH_TRACE_ROOT=%GIT67_SESSION_RAW_ROOT%"
set "SMARTAGENT_IT10_AUTO_PUBLISH=YES"
set "SMARTAGENT_IT10_GIT_TEST_REPO=b43120000/test"
echo.
echo [INIT] READY: IT10 raw trace enabled for marked requests only.
echo [INIT] Git67 branch: %GIT67_BRANCH%
git rev-parse HEAD
echo [INIT] Run ID: %SMARTAGENT_IT10_PATH_TRACE_RUN_ID%
echo [INIT] Private evidence: %SMARTAGENT_IT10_PATH_TRACE_ROOT%
echo [INIT] Auto upload: private b43120000/test, SAFE DERIVATIVE ONLY.
echo [INIT] Next: start WebCopilot and RemoteAgent FROM THIS CMD environment.
echo [INIT] Next: Live Telegram marked IT10 request.
echo [INIT] Next: collect.bat TASK-... RR-... 
echo [INIT] Original raw ZIP remains LOCAL; Git Test receives safe path byte excerpts.
exit /b 0

:init_failed
set "SMARTAGENT_IT10_AUTO_PUBLISH=OFF"
set "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED="
set "SMARTAGENT_IT10_PATH_TRACE="
set "SMARTAGENT_IT10_PATH_TRACE_RAW="
set "SMARTAGENT_IT10_PATH_TRACE_RUN_ID="
set "SMARTAGENT_IT10_PATH_TRACE_ROOT="
echo [INIT] NOT READY; RemoteAgent must not be launched for this diagnostic session.
exit /b 4
