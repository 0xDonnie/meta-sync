@echo off
cd /d "D:\GitHub\camera-sync"
python -u camera_sync.py --offline
echo.
echo ==============================
echo Sync Camera finished. Press any key to close.
pause > nul
