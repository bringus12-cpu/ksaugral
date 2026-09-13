@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_signal_bot.ps1"
echo.
echo Okno zostaje otwarte, zebys mogl zobaczyc komunikaty.
pause
