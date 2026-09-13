$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$processes = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like "python*.exe" -and
    $_.CommandLine -like "*run_agent_teams_profile.py*" -and
    $_.CommandLine -like "*$projectDir*"
}
foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output "Agent Teams stopped: $($processes.ProcessId -join ', ')"

