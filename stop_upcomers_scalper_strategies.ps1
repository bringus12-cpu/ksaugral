$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$profileNames = @(
    ".env.upcomers.scalp.hours", ".env.upcomers.scalp.micro"
)

Get-CimInstance Win32_Process | Where-Object {
    $process = $_
    $matchesProfile = @($profileNames | Where-Object { $process.CommandLine -match [regex]::Escape($_) }).Count -gt 0
    $process.Name -match "python" -and $process.CommandLine -match "run_xau_scalp_bot_profile.py" -and $matchesProfile
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
