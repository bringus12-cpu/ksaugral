$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $project ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$dashboardScript = Join-Path $project "run_dashboard.py"
$url = "http://127.0.0.1:8791"

function Test-Dashboard {
  try {
    Invoke-WebRequest -UseBasicParsing "$url/api/overview" -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

if (Test-Dashboard) {
  Write-Output "Dashboard juz dziala."
  Write-Output "URL: $url"
  Start-Process $url
  exit 0
}

$process = Start-Process -WindowStyle Hidden `
  -FilePath $python `
  -ArgumentList "-u", "`"$dashboardScript`"" `
  -WorkingDirectory $project `
  -PassThru

for ($i = 0; $i -lt 10; $i++) {
  Start-Sleep -Seconds 1
  if (Test-Dashboard) {
    Write-Output "Dashboard started. PID: $($process.Id)"
    Write-Output "URL: $url"
    Start-Process $url
    exit 0
  }
}

Write-Output "Dashboard nie odpowiedzial po starcie."
Write-Output "Uruchom recznie, zeby zobaczyc blad:"
Write-Output "$python -u $dashboardScript"
exit 1
