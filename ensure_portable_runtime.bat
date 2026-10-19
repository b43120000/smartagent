@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_portable_runtime.ps1" -ProjectRoot "%~dp0."
exit /b %ERRORLEVEL%
