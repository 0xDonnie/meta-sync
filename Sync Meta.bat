@echo off
cd /d "%~dp0"
python -u watcher.py --once
echo.
echo ==============================
echo Sync Meta finished. Press any key to close.
pause > nul
