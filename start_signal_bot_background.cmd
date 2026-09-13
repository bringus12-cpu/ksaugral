@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_signal_bot_background.ps1"
if errorlevel 1 pause
