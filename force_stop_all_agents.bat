@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
for %%I in ("%~dp0.") do set "SMARTAGENT_INSTALL_ROOT=%%~fI"
if not exist "%SMARTAGENT_INSTALL_ROOT%\config\protocol_manifest.json" (
    echo [SmartAgent] ERROR install_root_missing_protocol_manifest: "%SMARTAGENT_INSTALL_ROOT%"
    exit /b 2
)
set "SMARTAGENT_SOURCE=%~dp0source"
if not exist "%SMARTAGENT_SOURCE%\agent_core\__init__.py" (
    echo [SmartAgent] ERROR Canonical source is missing: %SMARTAGENT_SOURCE%
    exit /b 2
)
set "PYTHONPATH=%SMARTAGENT_SOURCE%;%PYTHONPATH%"
set "PYTHONSAFEPATH=1"
title SmartAgent - Force Stop All Agents

if /I "%~1"=="--launcher-self-test" (
    echo FORCE_STOP_ALL_AGENTS_LAUNCHER_PARSE_OK
    exit /b 0
)

set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" (
    echo [SmartAgent] ERROR This SmartAgent folder has no installed runtime: %SMARTAGENT_PYTHON%
    exit /b 2
)

set "NO_PAUSE="
set "EXCLUDE_PID_ARGS="
set "AFTER_UPDATE_ARGS="
set "GIT_SOURCE_ARGS="
set "RECOVER_TURBO_ROOT="
set "LAUNCHER_SELF_TEST="
set "INIT_ARGS="
:parse_args
if "%~1"=="" goto args_parsed
if /I "%~1"=="--init" (
    set "INIT_ARGS=--init"
    shift /1
    goto parse_args
)
if /I "%~1"=="--launcher-self-test" (
    set "LAUNCHER_SELF_TEST=1"
    shift /1
    goto parse_args
)
if /I "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
    shift /1
    goto parse_args
)
if /I "%~1"=="--exclude-pid" (
    if "%~2"=="" (
        echo [SmartAgent] ERROR --exclude-pid requires a PID.
        endlocal & exit /b 64
    )
    set "EXCLUDE_PID_ARGS=%EXCLUDE_PID_ARGS% --exclude-pid %~2"
    shift /1
    shift /1
    goto parse_args
)
if /I "%~1"=="--recover-turbo-import" (
    if "%~2"=="" (
        echo [SmartAgent] ERROR --recover-turbo-import requires an exact Turbo Root.
        endlocal & exit /b 64
    )
    set "RECOVER_TURBO_ROOT=%~2"
    shift /1
    shift /1
    goto parse_args
)

if /I "%~1"=="--git-source-root" (
    if "%~2"=="" (
        echo [SmartAgent] ERROR --git-source-root requires a checkout path.
        endlocal & exit /b 64
    )
    set GIT_SOURCE_ARGS=--git-source-root "%~2"
    shift /1
    shift /1
    goto parse_args
)

if /I "%~1"=="--after-git-update" (
    if "%~2"=="" (
        echo [SmartAgent] ERROR --after-git-update requires a full commit SHA.
        endlocal & exit /b 64
    )
    set AFTER_UPDATE_ARGS=--after-git-update "%~2"
    shift /1
    shift /1
    goto parse_args
)
echo [SmartAgent] ERROR Unknown argument: %~1
endlocal & exit /b 64

:args_parsed
if defined LAUNCHER_SELF_TEST (
    for %%I in ("%~dp0.") do if /I not "%%~fI"=="%SMARTAGENT_INSTALL_ROOT%" (
        echo FORCE_STOP_ALL_AGENTS_LAUNCHER_ROOT_CHANGED
        endlocal & exit /b 2
    )
    echo FORCE_STOP_ALL_AGENTS_LAUNCHER_PARSE_OK %EXCLUDE_PID_ARGS%
    echo FORCE_STOP_RECOVERY_SCRIPT=%SMARTAGENT_INSTALL_ROOT%\install_smart_agent\turbo_pending_import_recovery.ps1
    echo FORCE_STOP_INIT_ARGS=%INIT_ARGS%
    endlocal & exit /b 0
)
echo [SmartAgent] Stopping LocalAgent, WebAgent, RemoteAgent, monitors and workers...
echo [SmartAgent] Install Root: "%SMARTAGENT_INSTALL_ROOT%"
set "REQUIRE_STARTUP_ARGS="
if defined RECOVER_TURBO_ROOT set "REQUIRE_STARTUP_ARGS=--require-startup-contract"
"%SMARTAGENT_PYTHON%" -m agent_core.force_stop_all_agents --root "%SMARTAGENT_INSTALL_ROOT%" %EXCLUDE_PID_ARGS% %AFTER_UPDATE_ARGS% %GIT_SOURCE_ARGS% %REQUIRE_STARTUP_ARGS% %INIT_ARGS%
set "STOP_EXIT=%errorlevel%"
if "%STOP_EXIT%"=="0" (
    echo [SmartAgent] All Agent processes stopped; owned pending work archived and cancelled.
    echo [SmartAgent] INIT verified: Runtime IDLE. Start RemoteAgent, then reopen the Workspace manager for a new update.
) else (
    echo [SmartAgent] Force stop completed with errors. exit_code=%STOP_EXIT%
)

if defined RECOVER_TURBO_ROOT (
    if not "%STOP_EXIT%"=="0" (
        echo [SmartAgent Turbo] Recovery BLOCKED: force stop or Git upgrade did not pass.
    ) else (
        call :perform_native_recovery
        if errorlevel 1 (
            echo [SmartAgent Turbo] Recovery FAILED. Preserve Journal and inspect recovery log.
            set "STOP_EXIT=2"
        ) else (
            echo [SmartAgent Turbo] Native Recovery verified by elevated exit receipt.
        )
    )
)
if not defined NO_PAUSE pause
endlocal & exit /b %STOP_EXIT%

:perform_native_recovery
set "SMARTAGENT_RECOVERY_SCRIPT=%SMARTAGENT_INSTALL_ROOT%\install_smart_agent\turbo_pending_import_recovery.ps1"
if not exist "%SMARTAGENT_RECOVERY_SCRIPT%" (
    echo [SmartAgent Turbo] Recovery BLOCKED: recovery_script_missing: "%SMARTAGENT_RECOVERY_SCRIPT%"
    exit /b 3
)
echo [SmartAgent Turbo] Explicit Native Recovery requested for "%RECOVER_TURBO_ROOT%".
set "SMARTAGENT_RECOVERY_LOG=%TEMP%\smartagent_native_recovery_%RANDOM%_%RANDOM%.txt"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SMARTAGENT_RECOVERY_SCRIPT%" -ProjectRoot "%SMARTAGENT_INSTALL_ROOT%" -TurboRoot "%RECOVER_TURBO_ROOT%" >"%SMARTAGENT_RECOVERY_LOG%" 2>&1
set "SMARTAGENT_RECOVERY_EXIT=%errorlevel%"
type "%SMARTAGENT_RECOVERY_LOG%"
if not "%SMARTAGENT_RECOVERY_EXIT%"=="0" (
    echo [SmartAgent Turbo] Recovery process failed, exit_code=%SMARTAGENT_RECOVERY_EXIT%. Log: "%SMARTAGENT_RECOVERY_LOG%"
    exit /b 2
)
findstr /c:"TURBO_RECOVERY_ELEVATED_EXIT_PASS" "%SMARTAGENT_RECOVERY_LOG%" >nul
if errorlevel 1 (
    echo [SmartAgent Turbo] Recovery FAIL: native success receipt missing. Log: "%SMARTAGENT_RECOVERY_LOG%"
    exit /b 2
)
echo [SmartAgent Turbo] Native recovery receipt verified. Log: "%SMARTAGENT_RECOVERY_LOG%"
exit /b 0
