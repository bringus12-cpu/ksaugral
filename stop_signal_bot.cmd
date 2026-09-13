@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_signal_bot.ps1"
if errorlevel 1 pause
