$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$profiles = @("hours", "micro")

foreach ($profile in $profiles) {
    $stdout = Join-Path $project "xau_scalp_bot.upcomers.$profile.out.log"
    $stderr = Join-Path $project "xau_scalp_bot.upcomers.$profile.err.log"
    Start-Process -FilePath $python -ArgumentList @("-u", "run_xau_scalp_bot_profile.py", ".env.upcomers", ".env.upcomers.scalp.$profile") -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
}
