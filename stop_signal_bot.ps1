$ErrorActionPreference = "SilentlyContinue"

$project = (Split-Path -Parent $MyInvocation.MyCommand.Path).Replace("\", "\\")
$target = "run_signal_bot.py"

Get-CimInstance Win32_Process |
  Where-Object {
    $cmd = $_.CommandLine
    $_.Name -eq "python.exe" -and
    $cmd -match $project -and
    $cmd -match [regex]::Escape($target)
  } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Output ("Stopped PID " + $_.ProcessId)
  }
