$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-CimInstance Win32_Process | Where-Object {
    $process = $_
    $process.Name -match "python" -and
    $process.CommandLine -match "run_xau_scalp_bot_profile.py" -and
    $process.CommandLine -match [regex]::Escape(".env.vantage")
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
