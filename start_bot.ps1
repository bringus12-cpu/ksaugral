$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $project ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$outLog = Join-Path $project "bot.out.log"
$errLog = Join-Path $project "bot.err.log"
$botScript = Join-Path $project "run_bot.py"

& $python (Join-Path $project "configure.py")

$command = "$python -u $botScript > $outLog 2> $errLog"
$process = Start-Process -WindowStyle Hidden `
  -FilePath "cmd.exe" `
  -ArgumentList "/c", $command `
  -WorkingDirectory $project `
  -PassThru

Write-Output "Bot started. PID: $($process.Id)"
Write-Output "STDOUT: $outLog"
Write-Output "STDERR: $errLog"
