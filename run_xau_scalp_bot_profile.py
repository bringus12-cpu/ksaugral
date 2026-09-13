from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _load_profile_env() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: run_xau_scalp_bot_profile.py <base-env> [strategy-env]")
    for raw_path in sys.argv[1:]:
        env_path = Path(raw_path).resolve()
        if not env_path.exists():
            raise SystemExit(f"Env file not found: {env_path}")
        load_dotenv(env_path, override=True)


_load_profile_env()

from app.xau_scalp_bot import run
from app.process_supervisor import run_forever


if __name__ == "__main__":
    enabled = str(os.getenv("XAU_SCALP_ENABLED", "true") or "true").strip().lower()
    if enabled in {"1", "true", "yes", "on"}:
        run_forever(run, "xau-scalper")
    else:
        run()
