@echo off
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\ensure_environment.ps1" -ProjectRoot "%~dp0..\.."
exit /b %ERRORLEVEL%
