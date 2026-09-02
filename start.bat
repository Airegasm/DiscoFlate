@echo off
REM DiscoFlate launcher for Windows. Double-click, or run from a terminal.
setlocal
cd /d "%~dp0"

REM --- find Python (prefer the py launcher, fall back to python) ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo IMPORTANT: on the first installer screen, check "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

REM --- create the virtualenv on first run, then install deps ---
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment ^(.venv^) ...
  %PY% -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
) else (
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)

echo.
echo DiscoFlate starting on http://127.0.0.1:8765   (press Ctrl+C to stop)
echo Opening the control panel in your browser...
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" app.py

echo.
echo DiscoFlate has stopped.
pause
