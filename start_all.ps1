$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path

& (Join-Path $project "start_bot.ps1")
Start-Sleep -Seconds 2
& (Join-Path $project "start_dashboard.ps1")
Start-Sleep -Seconds 1
& (Join-Path $project "start_agent_teams.ps1")
Start-Sleep -Seconds 1
& (Join-Path $project "start_xau_long_term.ps1")
Start-Sleep -Seconds 1
& (Join-Path $project "start_vantage_indicator_strategies.ps1")
