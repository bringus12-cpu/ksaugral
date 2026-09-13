from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mt5_gateway import mt5
from scripts import backtest_xau_scalper_current as backtest


VARIANTS = (
    ("baseline", None, 1),
    ("micro_3_no_be", (0.10, 0.20, 0.30), 0),
    ("micro_3_be_all", (0.10, 0.20, 0.30), 3),
    ("micro_4_no_be", (0.10, 0.20, 0.30, 0.50), 0),
    ("micro_4_be_all", (0.10, 0.20, 0.30, 0.50), 4),
    ("fast_3_no_be", (0.15, 0.25, 0.40), 0),
    ("fast_3_be_all", (0.15, 0.25, 0.40), 3),
    ("quick_3_be_all", (0.40, 0.60, 0.80), 3),
    ("quick_3_no_be", (0.40, 0.60, 0.80), 0),
    ("balanced_3_be_all", (0.50, 0.75, 1.00), 3),
    ("balanced_3_be_first", (0.50, 0.75, 1.00), 1),
    ("runner_3_be_all", None, 3),
    ("runner_3_be_first", None, 1),
    ("runner_4_be_first", None, 1),
    ("runner_4_no_be", None, 0),
)


def _run_profile(
    env_file: str,
    profile: str,
    output_dir: Path,
    sessions: int,
    start_balance: float,
    commission: float,
    far_target_r: float,
) -> list[dict[str, Any]]:
    rate_cache: dict[str, Any] = {}
    signal_cache: dict[int, Any] = {}
    original_rates = backtest._rates
    original_signal = backtest._signal_from_rows
    original_shutdown = backtest.shutdown

    def cached_rates(symbol, timeframe, start, end):
        if timeframe not in rate_cache:
            rate_cache[timeframe] = original_rates(symbol, timeframe, start, end)
        return rate_cache[timeframe].copy()

    def cached_signal(frame, idx, cfg):
        if idx not in signal_cache:
            signal_cache[idx] = original_signal(frame, idx, cfg)
        return copy.deepcopy(signal_cache[idx])

    backtest._rates = cached_rates
    backtest._signal_from_rows = cached_signal
    backtest.shutdown = lambda: None
    rows: list[dict[str, Any]] = []
    try:
        for name, plan, protect_count in VARIANTS:
            if name == "baseline":
                targets = (far_target_r,)
            elif name.startswith("runner_3"):
                targets = (0.50, 0.85, far_target_r)
            elif name.startswith("runner_4"):
                targets = (0.40, 0.60, 0.85, far_target_r)
            else:
                targets = plan or (far_target_r,)
            output = output_dir / f"{Path(profile).stem}_{name}.json"
            argv = [
                "backtest_xau_scalper_current.py",
                "--env", env_file,
                "--strategy-env", profile,
                "--sessions", str(sessions),
                "--include-today",
                "--start-balance", str(start_balance),
                "--dynamic-spread",
                "--commission-per-001", str(commission),
                "--protect-leg-count", str(protect_count),
                "--override", f"XAU_SCALP_LEG_COUNT={len(targets)}",
                "--override", "XAU_SCALP_TARGET_R_PLAN=" + ",".join(str(value) for value in targets),
                "--output", str(output),
            ]
            old_argv = sys.argv
            sys.argv = argv
            try:
                backtest.main()
            finally:
                sys.argv = old_argv
            result = json.loads(output.read_text(encoding="utf-8"))
            rows.append(
                {
                    "variant": name,
                    "targets_r": list(targets),
                    "protect_leg_count": protect_count,
                    "result_file": str(output),
                    "final_balance": result["final_balance"],
                    "profit": result["profit"],
                    "profit_percent": result["profit_percent"],
                    "max_drawdown": result["max_drawdown_from_peak"],
                    "batches": result["batches"],
                    "legs_closed": result["legs_closed"],
                    "win_rate": result["win_rate_closed_legs_pct"],
                    "profit_factor": result["profit_factor"],
                    "average_win": result["average_win"],
                    "average_loss": result["average_loss"],
                    "spread_paid": result["spread_paid"],
                    "commission_paid": result["commission_paid"],
                }
            )
    finally:
        backtest._rates = original_rates
        backtest._signal_from_rows = original_signal
        backtest.shutdown = original_shutdown
        mt5.shutdown()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.puprime.live.frozen")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_puprime_live/research/active_scalper_multileg_exits.json")
    args = parser.parse_args()
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    profiles = (
        ("DB60", ".env.puprime.live.scalp.db60.frozen", 3.20),
        ("BBKelt", ".env.puprime.live.scalp.bbkelt.frozen", 1.80),
    )
    report: dict[str, Any] = {
        "method": (
            "Exact current-engine replay over completed sessions plus today's partial session; dynamic spread, "
            "commission, conservative SL-first ordering, and total setup risk split equally between legs."
        ),
        "sessions": args.sessions,
        "start_balance": args.start_balance,
        "profiles": {},
    }
    for label, profile, far_target in profiles:
        report["profiles"][label] = _run_profile(
            args.env,
            profile,
            output.parent / "active_scalper_multileg_runs",
            args.sessions,
            args.start_balance,
            args.commission_per_001,
            far_target,
        )
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
