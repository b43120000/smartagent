@echo off
for %%R in ("%~dp0..\..") do set "SMARTAGENT_PACKAGE_ROOT=%%~fR\"
call "%SMARTAGENT_PACKAGE_ROOT%install_smart_agent\launchers\restart_chatgpt.bat" %*
exit /b %errorlevel%
