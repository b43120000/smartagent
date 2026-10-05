@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
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
set "LAUNCHER_SELF_TEST="
:parse_args
if "%~1"=="" goto args_parsed
if /I "%~1"=="--launcher-self-test" (
    set "LAUNCHER_SELF_TEST=1"
    shift
    goto parse_args
)
if /I "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
    shift
    goto parse_args
)
if /I "%~1"=="--exclude-pid" (
    if "%~2"=="" (
        echo [SmartAgent] ERROR --exclude-pid requires a PID.
        endlocal & exit /b 64
    )
    set "EXCLUDE_PID_ARGS=%EXCLUDE_PID_ARGS% --exclude-pid %~2"
    shift
    shift
    goto parse_args
)
echo [SmartAgent] ERROR Unknown argument: %~1
endlocal & exit /b 64

:args_parsed
if defined LAUNCHER_SELF_TEST (
    echo FORCE_STOP_ALL_AGENTS_LAUNCHER_PARSE_OK %EXCLUDE_PID_ARGS%
    endlocal & exit /b 0
)
echo [SmartAgent] Stopping LocalAgent, WebAgent, RemoteAgent, monitors and workers...
"%SMARTAGENT_PYTHON%" -m agent_core.force_stop_all_agents --root "%~dp0." %EXCLUDE_PID_ARGS%
set "STOP_EXIT=%errorlevel%"
if "%STOP_EXIT%"=="0" (
    echo [SmartAgent] All discovered Agent connections and process trees are stopped.
) else (
    echo [SmartAgent] Force stop completed with errors. exit_code=%STOP_EXIT%
)

if not defined NO_PAUSE pause
endlocal & exit /b %STOP_EXIT%
