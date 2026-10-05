@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."
title RemoteAgent 0 - Stage 7 Supervisor

echo.
echo  +================================================+
echo  ^| RemoteAgent 0 - Stage 7 Supervisor            ^|
echo  ^| Durable queue: no worker / no tool execution     ^|
echo  +================================================+
echo.

where python >nul 2>&1
if %errorlevel%==0 (
    python RemoteAgent\remote_agent.py %*
    goto :done
)
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 RemoteAgent\remote_agent.py %*
    goto :done
)

echo [ERROR] Python not found in PATH.
exit /b 1

:done
set EXIT_CODE=%errorlevel%
echo.
echo [RemoteAgent 0] exited with code %EXIT_CODE%
exit /b %EXIT_CODE%
