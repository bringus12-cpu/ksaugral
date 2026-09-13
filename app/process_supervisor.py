from __future__ import annotations

import os
import sys
import time
import traceback
from collections.abc import Callable

from .mt5_gateway import shutdown


def run_forever(target: Callable[[], None], label: str) -> None:
    """Keep a bot profile alive across temporary MT5 or network outages."""
    delay = max(5.0, float(os.getenv("BOT_RESTART_DELAY_SECONDS", "60") or 60.0))
    while True:
        try:
            target()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            print(
                f"{timestamp} [SUPERVISOR] {label} stopped: {type(exc).__name__}: {exc}; retry in {delay:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            traceback.print_exc(file=sys.stderr)
            shutdown()
            time.sleep(delay)
