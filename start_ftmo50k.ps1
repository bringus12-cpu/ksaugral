$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$baseEnv = Join-Path $project ".env.vantage"
$fundedEnv = Join-Path $project ".env.ftmo50k"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python runtime not found: $python"
}

$settings = @{}
Get-Content -LiteralPath $fundedEnv | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        $settings[$matches[1].Trim()] = $matches[2].Trim()
    }
}

if (-not $settings.ContainsKey("MT5_LOGIN") -or $settings["MT5_LOGIN"] -eq "0") {
    throw "FTMO profile is not configured. Set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER and MT5_PATH in .env.ftmo50k."
}
if (-not $settings.ContainsKey("MT5_PASSWORD") -or $settings["MT5_PASSWORD"] -eq "NOT_CONFIGURED") {
    throw "FTMO password is not configured in .env.ftmo50k."
}
if (-not $settings.ContainsKey("MT5_SERVER") -or $settings["MT5_SERVER"] -eq "FTMO-NOT-CONFIGURED") {
    throw "FTMO server is not configured in .env.ftmo50k."
}
$terminal = $settings["MT5_PATH"] -replace '/', '\'
if (-not (Test-Path -LiteralPath $terminal)) {
    throw "FTMO terminal not found: $terminal"
}

$vantagePathLine = Get-Content -LiteralPath $baseEnv | Where-Object { $_ -match '^MT5_PATH=' } | Select-Object -First 1
if ($vantagePathLine) {
    $vantageTerminal = ($vantagePathLine -replace '^MT5_PATH=', '') -replace '/', '\'
    if ([System.IO.Path]::GetFullPath($terminal) -eq [System.IO.Path]::GetFullPath($vantageTerminal)) {
        throw "FTMO must use a separate terminal installation; MT5_PATH currently points to the Vantage terminal."
    }
}

$terminalProcess = Start-Process -FilePath $terminal -WorkingDirectory (Split-Path -Parent $terminal) -WindowStyle Hidden -PassThru
Write-Output "Started FTMO terminal PID $($terminalProcess.Id)."
Start-Sleep -Seconds 8

$jobs = @(
    @("signal", "run_signal_bot_profile.py", @(".env.vantage", ".env.ftmo50k")),
    @("adx07", "run_xau_scalp_bot_profile.py", @(".env.vantage", ".env.vantage.scalp.p300_adx07", ".env.ftmo50k", ".env.ftmo50k.scalp.adx07")),
    @("bbkelt", "run_xau_scalp_bot_profile.py", @(".env.vantage", ".env.vantage.scalp.p300_bbkelt", ".env.ftmo50k", ".env.ftmo50k.scalp.bbkelt")),
    @("bbrcl", "run_xau_scalp_bot_profile.py", @(".env.vantage", ".env.vantage.scalp.p300_bbrcl", ".env.ftmo50k", ".env.ftmo50k.scalp.bbrcl"))
)

foreach ($job in $jobs) {
    $name = $job[0]
    $runner = $job[1]
    $envArgs = $job[2]
    $identityEnv = $envArgs[-1]
    $existing = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*.exe" -and
        $_.CommandLine -like "*$runner*" -and
        $_.CommandLine -like "*$identityEnv*"
    }
    if ($existing) {
        Write-Output "$name already running: $($existing.ProcessId -join ', ')"
        continue
    }

    $stdout = Join-Path $project "ftmo50k.$name.out.log"
    $stderr = Join-Path $project "ftmo50k.$name.err.log"
    $arguments = @("-u", $runner) + $envArgs
    $process = Start-Process -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $project `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-Output "Started $name PID $($process.Id)."
}
