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
from app.risk import normalize_volume
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


def _parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_int_list(raw: str) -> list[int]:
    return [int(float(item.strip())) for item in raw.split(",") if item.strip()]


def _score(result: dict[str, Any]) -> tuple[float, float, float, float]:
    profit = float(result["profit"])
    dd = abs(float(result["max_drawdown_from_peak"]))
    win_rate = float(result["win_rate_closed_legs_pct"])
    legs = float(result["legs_closed"])
    profit_to_dd = profit / dd if dd > 0 else 999.0
    return (profit_to_dd, profit, win_rate, legs)


def _simulate(
    *,
    m1: pd.DataFrame,
    start_at: datetime,
    end_at: datetime,
    symbol: str,
    cfg: Any,
    start_balance: float,
    min_hold_seconds: float,
    tp1: float,
    tp2: float,
    sl: float,
    cooldown: int,
    m1_chase: float,
    m5_chase: float,
) -> dict[str, Any]:
    old_env = {
        "XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD": os.environ.get("XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD"),
        "XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD": os.environ.get("XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD"),
    }
    os.environ["XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD"] = str(m1_chase)
    os.environ["XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD"] = str(m5_chase)
    cfg = replace(
        cfg,
        xau_scalp_tp1_usd=float(tp1),
        xau_scalp_tp2_usd=float(tp2),
        xau_scalp_sl_usd=float(sl),
        xau_scalp_cooldown_seconds=int(cooldown),
    )

    balance = float(start_balance)
    peak = balance
    max_dd = 0.0
    next_allowed = _ts(start_at)
    open_legs: list[Leg] = []
    trades: list[dict[str, Any]] = []
    batches = 0
    triggers = {"buy": 0, "sell": 0}
    skipped = 0
    start_idx = _search_time(m1, start_at, side="left")

    try:
        for idx in range(max(80, start_idx), len(m1)):
            ts = m1.iloc[idx]["time"]
            if ts > _ts(end_at):
                break
            row = m1.iloc[idx]
            high = float(row["high"])
            low = float(row["low"])

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
                skipped += 1
                continue
            batches += 1
            triggers[signal["side"]] += 1
            next_allowed = ts + pd.Timedelta(seconds=int(cooldown))
            open_legs = [
                Leg(signal["side"], signal["entry"], signal["sl"], signal["tp1"], "tp1", idx),
                Leg(signal["side"], signal["entry"], signal["sl"], signal["tp2"], "tp2a", idx),
                Leg(signal["side"], signal["entry"], signal["sl"], signal["tp2"], "tp2b", idx),
            ]
            if min_hold_seconds > 0:
                hold_until = ts + pd.Timedelta(seconds=float(min_hold_seconds))
                hold_idx = min(int(_search_time(m1, hold_until, side="left")), len(m1) - 1)
                for leg in open_legs:
                    leg.min_hold_until_idx = hold_idx
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    by_status: dict[str, int] = {}
    for trade in trades:
        by_status[str(trade["status"])] = by_status.get(str(trade["status"]), 0) + 1
    closed = len(trades)
    non_loss = by_status.get("win", 0) + by_status.get("be", 0)
    profit = balance - float(start_balance)
    dd = abs(max_dd)
    return {
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "cooldown": cooldown,
        "m1_chase": m1_chase,
        "m5_chase": m5_chase,
        "final_balance": round(balance, 2),
        "profit": round(profit, 2),
        "profit_percent": round((profit / float(start_balance)) * 100.0, 2),
        "max_drawdown_from_peak": round(max_dd, 2),
        "profit_to_dd": round((profit / dd), 3) if dd > 0 else 999.0,
        "batches": batches,
        "legs_closed": closed,
        "win_rate_closed_legs_pct": round((non_loss / closed * 100.0), 2) if closed else 0.0,
        "by_status": by_status,
        "triggers": triggers,
        "skipped_bars": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.upcomers")
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--min-hold-seconds", type=float, default=125.0)
    parser.add_argument("--tp1", default="2.5,3.0,3.5,4.0,4.5,5.0")
    parser.add_argument("--tp2", default="3.8,5.0,6.0,7.0,8.0,9.0")
    parser.add_argument("--sl", default="3.5,4.0,4.5,5.0,6.0,7.0")
    parser.add_argument("--cooldown", default="300,600,900")
    parser.add_argument("--m1-chase", default="1.8,2.4,3.0")
    parser.add_argument("--m5-chase", default="3.0,3.5,4.5")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)

    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=float(args.days))
    warmup = timedelta(days=4)
    m1 = _rates(symbol, "M1", start_at - warmup, end_at + timedelta(hours=4))
    m5 = _rates(symbol, "M5", start_at - warmup, end_at + timedelta(hours=4))
    m15 = _rates(symbol, "M15", start_at - warmup, end_at + timedelta(hours=4))
    if m1.empty or m5.empty or m15.empty:
        raise RuntimeError("No rates returned from MT5")
    m1 = _align_frames(m1, m5, m15)

    results: list[dict[str, Any]] = []
    for tp1 in _parse_float_list(args.tp1):
        for tp2 in _parse_float_list(args.tp2):
            if tp2 <= tp1:
                continue
            for sl in _parse_float_list(args.sl):
                for cooldown in _parse_int_list(args.cooldown):
                    for m1_chase in _parse_float_list(args.m1_chase):
                        for m5_chase in _parse_float_list(args.m5_chase):
                            results.append(
                                _simulate(
                                    m1=m1,
                                    start_at=start_at,
                                    end_at=end_at,
                                    symbol=symbol,
                                    cfg=cfg,
                                    start_balance=float(args.start_balance),
                                    min_hold_seconds=float(args.min_hold_seconds),
                                    tp1=tp1,
                                    tp2=tp2,
                                    sl=sl,
                                    cooldown=cooldown,
                                    m1_chase=m1_chase,
                                    m5_chase=m5_chase,
                                )
                            )

    objective = load_analysis_objective()
    ranked_profit = sorted(results, key=lambda row: float(row["profit"]), reverse=True)
    ranked_objective = rank_profit_win(
        results,
        profit=lambda row: float(row["profit"]),
        win_rate=lambda row: float(row["win_rate_closed_legs_pct"]),
        objective=objective,
    )
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "symbol": symbol,
        "start_balance": round(float(args.start_balance), 2),
        "min_hold_seconds": float(args.min_hold_seconds),
        "tested": len(results),
        "analysis_objective": objective.as_dict(),
        "best_by_profit": ranked_profit[:25],
        "best_by_objective": ranked_objective[:25],
    }
    path = Path(args.output) if args.output else cfg.data_dir / f"xau_scalper_min_hold_optimization_{int(args.days)}d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    main()
