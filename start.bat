@echo off
cd /d "%~dp0"
echo Starting PulsedMedia Sniper...
powershell -ExecutionPolicy Bypass -File "%~dp0run_local.ps1" -Detached
echo.
pause
