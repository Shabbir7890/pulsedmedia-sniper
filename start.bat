@echo off
cd /d "%~dp0"
echo Starting Pulsed Media sniper...
powershell -ExecutionPolicy Bypass -File "%~dp0run_local.ps1" -Detached
echo.
pause
