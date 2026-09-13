$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $project ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$outLog = Join-Path $project "signal_bot.out.log"
$errLog = Join-Path $project "signal_bot.err.log"
$signalScript = Join-Path $project "run_signal_bot.py"

& $python (Join-Path $project "configure.py")

$authCheck = @'
import asyncio
from telethon import TelegramClient
from app.config import load_settings


async def main():
    cfg = load_settings()
    client = TelegramClient(str((cfg.data_dir / cfg.telegram_session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    authorized = await client.is_user_authorized()
    await client.disconnect()
    raise SystemExit(0 if authorized else 1)


asyncio.run(main())
'@

& $python -c $authCheck
if ($LASTEXITCODE -ne 0) {
  Write-Output "Telegram nie jest jeszcze zalogowany."
  Write-Output "Najpierw uruchom start_signal_bot.cmd i wpisz kod w widocznej konsoli."
  exit 1
}

$process = Start-Process -WindowStyle Hidden `
  -FilePath $python `
  -ArgumentList "-u", "`"$signalScript`"" `
  -WorkingDirectory $project `
  -RedirectStandardOutput $outLog `
  -RedirectStandardError $errLog `
  -PassThru

Write-Output "Signal bot started in background. PID: $($process.Id)"
Write-Output "Log: $(Join-Path $project "logs\telegram_signal_bot.log")"
Write-Output "STDOUT: $outLog"
Write-Output "STDERR: $errLog"
