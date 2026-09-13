$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$profiles = @(
    ".env.vantage.scalp.ind01",
    ".env.vantage.scalp.p300_bbrcl"
)

foreach ($profile in $profiles) {
    $existing = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*.exe" -and
        $_.CommandLine -like "*run_xau_scalp_bot_profile.py*" -and
        $_.CommandLine -like "*$profile*"
    }
    if ($existing) {
        Write-Output "$profile already running: $($existing.ProcessId -join ', ')"
        continue
    }

    $suffix = $profile.Replace(".env.vantage.scalp.", "")
    $stdout = Join-Path $project "xau_scalp_bot.vantage.$suffix.out.log"
    $stderr = Join-Path $project "xau_scalp_bot.vantage.$suffix.err.log"
    $process = Start-Process -FilePath $python `
        -ArgumentList @("-u", "run_xau_scalp_bot_profile.py", ".env.vantage", $profile) `
        -WorkingDirectory $project `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-Output "Started $profile PID $($process.Id)"
}
