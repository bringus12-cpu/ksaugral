$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$outLog = Join-Path $projectDir "agent_teams.vantage.out.log"
$errLog = Join-Path $projectDir "agent_teams.vantage.err.log"

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like "python*.exe" -and
    $_.CommandLine -like "*run_agent_teams_profile.py*" -and
    $_.CommandLine -like "*$projectDir*"
}
if ($existing) {
    Write-Output "Agent Teams already running: $($existing.ProcessId -join ', ')"
    exit 0
}

$process = Start-Process -WindowStyle Hidden -FilePath $python `
    -ArgumentList "-u", "run_agent_teams_profile.py", ".env.vantage", ".env.vantage.signal", ".env.vantage.agent_teams" `
    -WorkingDirectory $projectDir `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru
Write-Output "Agent Teams started, PID $($process.Id)"

