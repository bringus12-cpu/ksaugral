$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$scriptName = "run_xau_long_term_bot_profile.py"

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -like "*$scriptName*" -and
    $_.CommandLine -like "*$project*"
}
if ($existing) {
    Write-Output "XAU Long Term already running: $($existing.ProcessId -join ', ')"
    exit 0
}

$out = Join-Path $project "xau_long_term.vantage.out.log"
$err = Join-Path $project "xau_long_term.vantage.err.log"
$process = Start-Process -FilePath $python `
    -ArgumentList @('-u', $scriptName, '.env.vantage', '.env.vantage.long_term') `
    -WorkingDirectory $project `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -WindowStyle Hidden `
    -PassThru
Write-Output "Started XAU Long Term PID $($process.Id)"
