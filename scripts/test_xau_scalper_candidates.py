from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, shutdown
from app.strategy_objective import load_analysis_objective, rank_profit_win
from scripts.backtest_xau_scalper_current import (
    Leg,
    _align_frames,
    _better_stop,
    _hit_sl,
    _hit_tp,
    _lot_for_equity,
    _profit,
    _rates,
    _search_time,
    _signal_from_rows,
    _ts,
)


CANDIDATES = [
    {"name": "current", "tp1": 2.5, "tp2": 3.8, "sl": 4.0, "cooldown": 300, "m1_chase": 2.4, "m5_chase": 3.5},
    {"name": "hold_wider_tp", "tp1": 3.5, "tp2": 6.0, "sl": 4.5, "cooldown": 600, "m1_chase": 2.4, "m5_chase": 3.5},
    {"name": "hold_balanced", "tp1": 4.0, "tp2": 7.0, "sl": 5.0, "cooldown": 600, "m1_chase": 2.4, "m5_chase": 3.5},
    {"name": "hold_runner", "tp1": 4.0, "tp2": 8.0, "sl": 5.5, "cooldown": 600, "m1_chase": 2.4, "m5_chase": 3.5},
    {"name": "hold_conservative", "tp1": 3.5, "tp2": 6.0, "sl": 5.0, "cooldown": 900, "m1_chase": 2.0, "m5_chase": 3.0},
    {"name": "hold_high_win", "tp1": 3.0, "tp2": 5.0, "sl": 5.0, "cooldown": 900, "m1_chase": 1.8, "m5_chase": 3.0},
    {"name": "hold_looser_filter", "tp1": 4.0, "tp2": 7.0, "sl": 5.0, "cooldown": 600, "m1_chase": 3.0, "m5_chase": 4.5},
    {"name": "hold_tight_sl", "tp1": 3.5, "tp2": 6.0, "sl": 3.5, "cooldown": 600, "m1_chase": 2.4, "m5_chase": 3.5},
]


def _simulate(
    m1: pd.DataFrame,
    cfg: Any,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
    start_balance: float,
    min_hold_seconds: float,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    old_m1 = os.environ.get("XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD")
    old_m5 = os.environ.get("XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD")
    os.environ["XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD"] = str(candidate["m1_chase"])
    os.environ["XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD"] = str(candidate["m5_chase"])
    cfg = replace(
        cfg,
        xau_scalp_tp1_usd=float(candidate["tp1"]),
        xau_scalp_tp2_usd=float(candidate["tp2"]),
        xau_scalp_sl_usd=float(candidate["sl"]),
        xau_scalp_cooldown_seconds=int(candidate["cooldown"]),
    )
    try:
        balance = float(start_balance)
        peak = balance
        max_dd = 0.0
        next_allowed = _ts(start_at)
        open_legs: list[Leg] = []
        trades: list[dict[str, Any]] = []
        batches = 0
        start_idx = _search_time(m1, start_at, side="left")
        for idx in range(max(80, start_idx), len(m1)):
            ts = m1.iloc[idx]["time"]
            if ts > _ts(end_at):
                break
            high = float(m1.iloc[idx]["high"])
            low = float(m1.iloc[idx]["low"])
            for leg in list(open_legs):
                if leg.status != "open":
                    continue
                if leg.min_hold_until_idx >= 0 and idx < leg.min_hold_until_idx:
                    continue
                if _hit_tp(leg.side, leg.tp, high, low):
                    leg.status = "win"
                    leg.exit = leg.tp
                    leg.closed_idx = idx
                elif _hit_sl(leg.side, leg.sl, high, low):
                    leg.status = "be" if abs(leg.sl - leg.entry) < 0.05 else "loss"
                    leg.exit = leg.sl
                    leg.closed_idx = idx
            tp1_hit = any(leg.status == "win" and leg.leg == "tp1" and leg.closed_idx == idx for leg in open_legs)
            if tp1_hit:
                for leg in open_legs:
                    if leg.status == "open" and "tp2" in leg.leg:
                        leg.sl = _better_stop(leg.side, leg.sl, leg.entry)
            closed_now = [leg for leg in open_legs if leg.status != "open"]
            for leg in closed_now:
                profit_001 = _profit(symbol, leg.side, 0.01, leg.entry, leg.exit)
                objective = load_analysis_objective()
                if objective.ignore_position_size:
                    leg_lot = objective.normalized_leg_lot
                else:
                    _total_lot, leg_lot = _lot_for_equity(symbol, cfg, balance)
                profit = profit_001 * (leg_lot / 0.01)
                balance += profit
                peak = max(peak, balance)
                max_dd = min(max_dd, balance - peak)
                trades.append({"status": leg.status, "profit": profit})
            open_legs = [leg for leg in open_legs if leg.status == "open"]
            if open_legs or ts < next_allowed:
                continue
            signal, _diag = _signal_from_rows(m1, idx, cfg)
            if not signal:
                continue
            batches += 1
            next_allowed = ts + pd.Timedelta(seconds=int(candidate["cooldown"]))
            open_legs = [
                Leg(signal["side"], signal["entry"], signal["sl"], signal["tp1"], "tp1", idx),
                Leg(signal["side"], signal["entry"], signal["sl"], signal["tp2"], "tp2a", idx),
                Leg(signal["side"], signal["entry"], signal["sl"], signal["tp2"], "tp2b", idx),
            ]
            if min_hold_seconds > 0:
                hold_idx = min(int(_search_time(m1, ts + pd.Timedelta(seconds=min_hold_seconds), side="left")), len(m1) - 1)
                for leg in open_legs:
                    leg.min_hold_until_idx = hold_idx
        by_status: dict[str, int] = {}
        for trade in trades:
            by_status[trade["status"]] = by_status.get(trade["status"], 0) + 1
        closed = len(trades)
        non_loss = by_status.get("win", 0) + by_status.get("be", 0)
        profit = balance - float(start_balance)
        dd = abs(max_dd)
        return {
            **candidate,
            "final_balance": round(balance, 2),
            "profit": round(profit, 2),
            "profit_percent": round((profit / float(start_balance)) * 100.0, 2),
            "max_drawdown_from_peak": round(max_dd, 2),
            "profit_to_dd": round(profit / dd, 3) if dd else 999.0,
            "batches": batches,
            "legs_closed": closed,
            "win_rate_closed_legs_pct": round(non_loss / closed * 100.0, 2) if closed else 0.0,
            "by_status": by_status,
        }
    finally:
        if old_m1 is None:
            os.environ.pop("XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD", None)
        else:
            os.environ["XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD"] = old_m1
        if old_m5 is None:
            os.environ.pop("XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD", None)
        else:
            os.environ["XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD"] = old_m5


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.upcomers")
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--min-hold-seconds", type=float, default=125.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=float(args.days))
    warmup = timedelta(days=4)
    m1 = _align_frames(
        _rates(symbol, "M1", start_at - warmup, end_at + timedelta(hours=4)),
        _rates(symbol, "M5", start_at - warmup, end_at + timedelta(hours=4)),
        _rates(symbol, "M15", start_at - warmup, end_at + timedelta(hours=4)),
    )
    results = [_simulate(m1, cfg, symbol, start_at, end_at, float(args.start_balance), float(args.min_hold_seconds), item) for item in CANDIDATES]
    objective = load_analysis_objective()
    results_by_profit = sorted(results, key=lambda row: row["profit"], reverse=True)
    results_by_objective = rank_profit_win(
        results,
        profit=lambda row: float(row["profit"]),
        win_rate=lambda row: float(row["win_rate_closed_legs_pct"]),
        objective=objective,
    )
    out = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "symbol": symbol,
        "min_hold_seconds": float(args.min_hold_seconds),
        "analysis_objective": objective.as_dict(),
        "results_by_profit": results_by_profit,
        "results_by_objective": results_by_objective,
    }
    path = Path(args.output) if args.output else cfg.data_dir / f"xau_scalper_candidate_tests_{int(args.days)}d.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    main()
