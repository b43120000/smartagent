@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

title SmartAgent - Edit Workspace

set "SMARTAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%SMARTAGENT_PYTHON%" set "SMARTAGENT_PYTHON=python"

"%SMARTAGENT_PYTHON%" smart_agent.py --configure-startup

pause
endlocal
