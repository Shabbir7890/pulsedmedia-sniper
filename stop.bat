@echo off
cd /d "%~dp0"
if exist sniper.pid (
  for /f %%i in (sniper.pid) do (
    echo Stopping PID %%i ...
    taskkill /PID %%i /F
  )
  del sniper.pid
) else (
  echo No sniper.pid found - it may not be running.
)
echo.
pause
