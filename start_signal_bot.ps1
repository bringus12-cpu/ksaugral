$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $project ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$signalScript = Join-Path $project "run_signal_bot.py"

& $python (Join-Path $project "configure.py")

Write-Output ""
Write-Output "Uruchamiam bota sygnalowego w widocznej konsoli."
Write-Output "Jesli Telegram poprosi o kod, wpisz go tutaj."
Write-Output "Zatrzymanie: Ctrl+C"
Write-Output ""

& $python -u $signalScript
