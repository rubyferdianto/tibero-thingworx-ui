@echo off
rem Tibero SQL Viewer - one-click start (double-click this file)
setlocal
cd /d "%~dp0"
title Tibero SQL Viewer

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH. Install Python 3.9+ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

rem First run: install dependencies
python -c "import flask, requests, dotenv" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Could not install dependencies.
    pause
    exit /b 1
  )
)

rem First run: create .env from the template and let the user fill it in
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo Created .env from .env.example - fill in the ThingWorx values, save, then close Notepad.
  notepad ".env"
)

rem Port comes from APP_PORT in .env (default 5000)
set "APP_PORT_RUN=5000"
for /f "delims=" %%p in ('python -c "from dotenv import dotenv_values; print(dotenv_values('.env').get('APP_PORT') or 5000)"') do set "APP_PORT_RUN=%%p"

rem Stop a previous instance still listening on this port (python processes only)
set "FREED="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%APP_PORT_RUN% " ^| findstr "LISTENING"') do (
  tasklist /FI "PID eq %%a" /FO CSV /NH 2>nul | findstr /I "python" >nul
  if not errorlevel 1 (
    echo Stopping previous instance on port %APP_PORT_RUN% ^(PID %%a^)...
    taskkill /F /PID %%a >nul 2>nul
    set "FREED=1"
  ) else (
    echo [WARNING] Port %APP_PORT_RUN% is used by another program ^(PID %%a^). Change APP_PORT in .env.
  )
)
if defined FREED ping -n 2 127.0.0.1 >nul

if not defined NO_BROWSER (
  start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:%APP_PORT_RUN%"
)

echo.
echo Tibero SQL Viewer running at http://127.0.0.1:%APP_PORT_RUN%   (press Ctrl+C to stop)
echo.
python app.py

echo.
echo Server stopped.
pause
