@echo off
cd /d "%~dp0"
python -u report.py --local
echo.
echo ==============================
echo Report refreshed. Opening report.html...
start "" "%~dp0report.html"
