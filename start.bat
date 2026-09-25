@echo off
REM ---------------------------------------------------------------------------
REM  SentimentIQ launcher (Windows)
REM
REM      Double-click this file, or run:  start.bat
REM
REM  FIRST RUN:  creates the virtual environment and installs everything.
REM  EVERY RUN AFTER THAT: skips that and just starts the site.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
if "%DASHBOARD_PORT%"=="" set DASHBOARD_PORT=5001

REM -- First run only: create the virtual environment --------------------------
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup. This happens once and takes about 5 minutes.
  py -3.11 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" (
    echo ERROR: need Python 3.11 or 3.12 ^(3.13 is not supported yet^).
    echo        Install it from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
  )
)

REM -- First run only: install dependencies ------------------------------------
if not exist ".venv\.installed" (
  echo Installing dependencies ^(one time^) ...
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ERROR: install failed. Fix the error above and run this again.
    pause
    exit /b 1
  )
  echo installed > .venv\.installed
  echo Setup complete.
)

if not exist ".env" if exist ".env.example" copy .env.example .env >nul

echo Starting SentimentIQ on http://localhost:%DASHBOARD_PORT% ...
start "" http://localhost:%DASHBOARD_PORT%
.venv\Scripts\python run.py
pause
