$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Python venv not found: $python"
}

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "^python(w)?\.exe$" -and $_.CommandLine -like "*run_active_learning_committee.py*"
}
if ($existing) {
    Write-Output "Active Learning Committee already runs: $($existing.ProcessId -join ', ')"
    exit 0
}

$out = Join-Path $project "active_learning_committee.out.log"
$err = Join-Path $project "active_learning_committee.err.log"
Start-Process -FilePath $python `
    -ArgumentList "run_active_learning_committee.py" `
    -WorkingDirectory $project `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -WindowStyle Hidden
Write-Output "Active Learning Committee started"
