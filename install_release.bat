@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_VERSION=3.14"
set "PYTHON_RUNTIME=%~dp0python_runtime"

if exist "%PYTHON_RUNTIME%\python.exe" (
    echo [SmartAgent] Bundled Python runtime found.
) else (
    echo [SmartAgent] Python runtime is not installed.
    echo Please install Python %PYTHON_VERSION% and enable Add Python to PATH.
    echo After installation restart this installer.
    pause
    exit /b 1
)

setx SMARTAGENT_PYTHON "%PYTHON_RUNTIME%\python.exe" >nul

echo [SmartAgent] Runtime configured.
echo [SmartAgent] Python path:
echo %PYTHON_RUNTIME%\python.exe
pause
