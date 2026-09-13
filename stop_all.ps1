$ErrorActionPreference = "SilentlyContinue"

$project = (Split-Path -Parent $MyInvocation.MyCommand.Path).Replace("\", "\\")
$targets = @(
  "run_bot.py",
  "run_dashboard.py",
  "run_signal_bot.py",
  "run_signal_bot_profile.py",
  "run_xau_scalp_bot_profile.py",
  "run_agent_teams_profile.py"
)

Get-CimInstance Win32_Process |
  Where-Object {
    $cmd = $_.CommandLine
    $_.Name -eq "python.exe" -and
    $cmd -match $project -and
    (($targets | Where-Object { $cmd -match [regex]::Escape($_) }).Count -gt 0)
  } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Output ("Stopped PID " + $_.ProcessId)
  }
