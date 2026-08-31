@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo [SmartAgent] install_release.bat is a compatibility entry.
echo [SmartAgent] Redirecting to the environment checker and installer...
call "%~dp0install_smart_agent.bat" %*
exit /b %ERRORLEVEL%
