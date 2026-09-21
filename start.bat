@echo off
REM DiscoFlate launcher for Windows. Double-click, or run from a terminal.
setlocal
cd /d "%~dp0"

set "PORT=%DISCOFLATE_PORT%"
if not defined PORT set "PORT=8765"

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

REM --- DiscoFlate needs Python 3.9+ (discord.py 2.x floor) ---
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo DiscoFlate needs Python 3.9 or newer. Install a current Python 3 and retry.
  echo.
  pause
  exit /b 1
)

REM --- create the virtualenv on first run; reinstall deps only when
REM     requirements.txt changed (the .deps-ok marker mirrors start.sh) ---
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment ^(.venv^) ...
  %PY% -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  call :pip_sync
  if errorlevel 1 goto :pipfail
  type nul > ".venv\.deps-ok"
) else (
  set "NEEDS_DEPS="
  if not exist ".venv\.deps-ok" set "NEEDS_DEPS=1"
  if exist ".venv\.deps-ok" (
    REM xcopy /L /D lists requirements.txt only if it's newer than the marker:
    REM 2 output lines when newer, 1 when up to date.
    for /f %%i in ('xcopy /L /D /Y requirements.txt ".venv\.deps-ok" ^| find /c /v ""') do if %%i GEQ 2 set "NEEDS_DEPS=1"
  )
  if defined NEEDS_DEPS (
    echo Syncing dependencies ...
    call :pip_sync
    if errorlevel 1 goto :pipfail
    type nul > ".venv\.deps-ok"
  )
)

echo.
echo DiscoFlate starting on http://127.0.0.1:%PORT%   (press Ctrl+C to stop)
echo Opening the control panel in your browser...
start "" "http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" app.py

echo.
echo DiscoFlate has stopped.
pause
exit /b 0

:pip_sync
REM pip hygiene + install:
REM  * delete '~xyz' corpse folders that interrupted installs leave in
REM    site-packages (they cause "Ignoring invalid distribution" warnings)
REM  * install requirements; if that fails, purge pip's download cache and
REM    retry once (a corrupted cache is the usual silent culprit)
REM  * if the log shows cache-corruption warnings even on success, purge the
REM    cache so future runs are quiet
for /d %%D in (".venv\Lib\site-packages\~*") do rd /s /q "%%D" 2>nul
set "PIPLOG=%TEMP%\discoflate-pip.log"
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt > "%PIPLOG%" 2>&1
if errorlevel 1 (
  echo    pip hit trouble — cleaning its cache and retrying once ...
  ".venv\Scripts\python.exe" -m pip cache purge >nul 2>&1
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt > "%PIPLOG%" 2>&1
  if errorlevel 1 (
    type "%PIPLOG%"
    exit /b 1
  )
)
findstr /c:"Cache entry deserialization failed" "%PIPLOG%" >nul 2>nul
if not errorlevel 1 (
  echo    cleaning a corrupted pip cache ...
  ".venv\Scripts\python.exe" -m pip cache purge >nul 2>&1
)
exit /b 0

:pipfail
echo.
echo !! pip install failed — the log above shows why. Common fixes: check your
echo !! internet connection, then delete the .venv folder and run this again.
pause
exit /b 1
