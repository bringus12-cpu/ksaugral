$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$profiles = @(
    @{
        Env = ".env.vantage.scalp.db60.optimized"
        Out = "xau_scalp_bot.vantage.db60.optimized.out.log"
        Err = "xau_scalp_bot.vantage.db60.optimized.err.log"
    }
)

foreach ($profile in $profiles) {
    Start-Process -FilePath $python `
        -ArgumentList @("-u", "run_xau_scalp_bot_profile.py", ".env.vantage", $profile.Env) `
        -WorkingDirectory $project `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $project $profile.Out) `
        -RedirectStandardError (Join-Path $project $profile.Err)
}
