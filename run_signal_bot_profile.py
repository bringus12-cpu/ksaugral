from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _load_profile_env() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: run_signal_bot_profile.py <base-env> [override-env]")
    for raw_path in sys.argv[1:]:
        env_path = Path(raw_path).resolve()
        if not env_path.exists():
            raise SystemExit(f"Env file not found: {env_path}")
        load_dotenv(env_path, override=True)


_load_profile_env()

from app.telegram_signal_bot import run
from app.process_supervisor import run_forever


if __name__ == "__main__":
    run_forever(run, "telegram-signal-listener")
