@echo off
REM Repair a DiscoFlate clone made before the 2026-09 history rewrite.
REM Save this file into your DiscoFlate folder and double-click it.
REM Your data\ folder (token, config, leaderboard, backups) is NOT touched.
setlocal
cd /d "%~dp0"
if not exist app.py cd ..
if not exist app.py (
  echo !! Put this file in your DiscoFlate folder ^(next to app.py^) and run it again.
  pause
  exit /b 1
)
echo Fetching the rewritten history ...
git fetch origin || goto :fail
echo Adopting origin/main (data\ and .venv are untouched) ...
git reset --hard origin/main || goto :fail
echo Done - your clone is on the new history. Start DiscoFlate normally.
pause
exit /b 0
:fail
echo !! git failed - is git installed and is this a DiscoFlate clone?
pause
exit /b 1
