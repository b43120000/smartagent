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
title SmartAgent - Workspace Manager

set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if /I "%~1"=="--launcher-self-test" (
    if not exist "%SMARTAGENT_PYTHON%" (
        echo [Workspace] ERROR SmartAgent runtime is missing: %SMARTAGENT_PYTHON%
        exit /b 2
    )
    "%SMARTAGENT_PYTHON%" -B -c "import agent_core.workspace_manager; print('EDIT_WORKSPACE_LAUNCHER_PARSE_OK')"
    exit /b %ERRORLEVEL%
)
if exist "%SMARTAGENT_PYTHON%" goto run_manager

echo [SmartAgent] ERROR: This SmartAgent folder is not installed yet.
echo Missing: %SMARTAGENT_PYTHON%
echo Run install_smart_agent.bat in this folder first.
pause
exit /b 2

:run_manager
set "SMARTAGENT_APPLY_LOG="
for /f "delims=" %%P in ('""%SMARTAGENT_PYTHON%" -m agent_core.path_cli workspace_manager_launcher_log --root "%~dp0.""') do set "SMARTAGENT_APPLY_LOG=%%P"
if not defined SMARTAGENT_APPLY_LOG (
    echo [Workspace] ERROR Unable to resolve workspace manager launcher log path.
    exit /b 2
)
for %%D in ("%SMARTAGENT_APPLY_LOG%") do if not exist "%%~dpD" mkdir "%%~dpD" >nul 2>&1
>>"%SMARTAGENT_APPLY_LOG%" echo ==================================================
>>"%SMARTAGENT_APPLY_LOG%" echo [%date% %time%] BAT_START %*
>>"%SMARTAGENT_APPLY_LOG%" echo [%date% %time%] PYTHON=%SMARTAGENT_PYTHON%
echo [INFO] Starting SmartAgent Workspace Manager...
if "%~1"=="" (
    rem Keep the interactive menu visible; the manager writes crash details to its own log.
    "%SMARTAGENT_PYTHON%" -m agent_core.workspace_manager
) else (
    "%SMARTAGENT_PYTHON%" -m agent_core.workspace_manager %* >>"%SMARTAGENT_APPLY_LOG%" 2>&1
)
set "SMARTAGENT_EXIT=%errorlevel%"
>>"%SMARTAGENT_APPLY_LOG%" echo [%date% %time%] BAT_EXIT code=%SMARTAGENT_EXIT%
type "%SMARTAGENT_APPLY_LOG%"
if not "%SMARTAGENT_EXIT%"=="0" (
    echo [ERROR] workspace manager exit code: %SMARTAGENT_EXIT%
)
echo [INFO] Log file: %SMARTAGENT_APPLY_LOG%
pause
endlocal & exit /b %SMARTAGENT_EXIT%
