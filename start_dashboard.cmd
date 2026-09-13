@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_dashboard.ps1"
echo.
echo Jesli przegladarka sie nie otworzyla, wejdz recznie na:
echo http://127.0.0.1:8791
echo.
pause
