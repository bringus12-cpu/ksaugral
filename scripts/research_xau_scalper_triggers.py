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
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.strategy_objective import load_analysis_objective, rank_profit_win
from scripts.backtest_xau_scalper_current import _align_frames, _rates, _search_time, _signal_from_rows, _ts


CANDIDATES = [
    {"name": "s01_ema_cross", "mode": "ema_cross_fast", "tp1": 1.5, "tp2": 1.5, "sl": 2.0, "be": 99.0, "cooldown": 90, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s02_trend_continue", "mode": "trend_continuation_fast", "tp1": 2.0, "tp2": 2.0, "sl": 2.5, "be": 99.0, "cooldown": 120, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s03_momentum", "mode": "momentum_impulse", "tp1": 2.0, "tp2": 2.0, "sl": 2.5, "be": 99.0, "cooldown": 120, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s04_bb_reclaim", "mode": "bollinger_reclaim", "tp1": 1.5, "tp2": 1.5, "sl": 2.0, "be": 99.0, "cooldown": 90, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s05_liquidity_sweep", "mode": "liquidity_sweep", "tp1": 2.0, "tp2": 2.0, "sl": 2.5, "be": 99.0, "cooldown": 180, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s06_onebar_break", "mode": "one_bar_breakout", "tp1": 2.0, "tp2": 2.0, "sl": 2.5, "be": 99.0, "cooldown": 120, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s07_rsi_reversal", "mode": "rsi_reversal", "tp1": 1.5, "tp2": 1.5, "sl": 2.0, "be": 99.0, "cooldown": 90, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s08_vol_expansion", "mode": "volatility_expansion", "tp1": 2.0, "tp2": 2.0, "sl": 2.5, "be": 99.0, "cooldown": 150, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s09_micro_ema", "mode": "micro_ema_bounce", "tp1": 1.25, "tp2": 1.25, "sl": 1.75, "be": 99.0, "cooldown": 60, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "s10_micro_mean", "mode": "micro_mean_revert", "tp1": 1.25, "tp2": 1.25, "sl": 1.75, "be": 99.0, "cooldown": 60, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "trend_current", "mode": "trend_pullback", "tp1": 2.0, "tp2": 3.5, "sl": 4.0, "be": 0.9, "cooldown": 300, "env": {}},
    {"name": "trend_adx12", "mode": "trend_pullback", "tp1": 2.0, "tp2": 3.5, "sl": 4.0, "be": 0.9, "cooldown": 300, "env": {"XAU_SCALP_MIN_M5_ADX": "12"}},
    {"name": "trend_adx10", "mode": "trend_pullback", "tp1": 2.0, "tp2": 3.5, "sl": 4.0, "be": 0.9, "cooldown": 300, "env": {"XAU_SCALP_MIN_M5_ADX": "10"}},
    {"name": "trend_sl3", "mode": "trend_pullback", "tp1": 2.0, "tp2": 3.5, "sl": 3.0, "be": 0.9, "cooldown": 300, "env": {}},
    {"name": "trend_sl3_all_tp1", "mode": "trend_pullback", "tp1": 2.0, "tp2": 3.5, "sl": 3.0, "be": 0.9, "cooldown": 300, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "trend_sl3_all_tp25", "mode": "trend_pullback", "tp1": 2.5, "tp2": 3.5, "sl": 3.0, "be": 1.0, "cooldown": 300, "plan": "tp1,tp1,tp1", "env": {}},
    {"name": "trend_rr", "mode": "trend_pullback", "tp1": 2.5, "tp2": 4.5, "sl": 3.5, "be": 1.2, "cooldown": 300, "env": {}},
    {"name": "range_tight", "mode": "range_reversion", "tp1": 1.5, "tp2": 2.5, "sl": 3.0, "be": 1.0, "cooldown": 300, "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "16", "XAU_SCALP_RANGE_RSI_EDGE": "32", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.45"}},
    {"name": "range_balanced", "mode": "range_reversion", "tp1": 2.0, "tp2": 3.5, "sl": 4.0, "be": 1.2, "cooldown": 300, "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "18", "XAU_SCALP_RANGE_RSI_EDGE": "35", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.55"}},
    {"name": "range_wide", "mode": "range_reversion", "tp1": 2.5, "tp2": 4.0, "sl": 4.5, "be": 1.5, "cooldown": 600, "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "20", "XAU_SCALP_RANGE_RSI_EDGE": "38", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.70"}},
    {"name": "range_wide_sl35", "mode": "range_reversion", "tp1": 2.5, "tp2": 4.0, "sl": 3.5, "be": 1.2, "cooldown": 600, "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "20", "XAU_SCALP_RANGE_RSI_EDGE": "38", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.70"}},
    {"name": "range_wide_rr", "mode": "range_reversion", "tp1": 3.0, "tp2": 5.0, "sl": 4.0, "be": 1.5, "cooldown": 600, "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "20", "XAU_SCALP_RANGE_RSI_EDGE": "38", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.70"}},
    {"name": "range_wide_all_tp1", "mode": "range_reversion", "tp1": 2.0, "tp2": 3.5, "sl": 3.0, "be": 1.0, "cooldown": 600, "plan": "tp1,tp1,tp1", "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "20", "XAU_SCALP_RANGE_RSI_EDGE": "38", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.70"}},
    {"name": "range_wide_all_tp25", "mode": "range_reversion", "tp1": 2.5, "tp2": 3.5, "sl": 3.0, "be": 1.0, "cooldown": 600, "plan": "tp1,tp1,tp1", "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "20", "XAU_SCALP_RANGE_RSI_EDGE": "38", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.70"}},
    {"name": "range_wide_all_tp3", "mode": "range_reversion", "tp1": 3.0, "tp2": 4.0, "sl": 3.0, "be": 1.2, "cooldown": 600, "plan": "tp1,tp1,tp1", "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "20", "XAU_SCALP_RANGE_RSI_EDGE": "38", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.70"}},
    {"name": "range_selective", "mode": "range_reversion", "tp1": 1.8, "tp2": 3.0, "sl": 3.5, "be": 1.0, "cooldown": 600, "env": {"XAU_SCALP_RANGE_MAX_M5_ADX": "14", "XAU_SCALP_RANGE_RSI_EDGE": "30", "XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR": "0.40"}},
    {"name": "breakout_fast", "mode": "range_breakout", "tp1": 2.0, "tp2": 4.0, "sl": 4.0, "be": 1.2, "cooldown": 600, "env": {"XAU_SCALP_BREAKOUT_MIN_M5_ADX": "14", "XAU_SCALP_BREAKOUT_MIN_BODY_ATR": "0.50", "XAU_SCALP_BREAKOUT_MIN_VOLUME_RATIO": "1.00"}},
    {"name": "breakout_balanced", "mode": "range_breakout", "tp1": 2.5, "tp2": 5.0, "sl": 4.5, "be": 1.5, "cooldown": 600, "env": {"XAU_SCALP_BREAKOUT_MIN_M5_ADX": "16", "XAU_SCALP_BREAKOUT_MIN_BODY_ATR": "0.65", "XAU_SCALP_BREAKOUT_MIN_VOLUME_RATIO": "1.10"}},
    {"name": "breakout_strict", "mode": "range_breakout", "tp1": 3.0, "tp2": 6.0, "sl": 5.0, "be": 1.8, "cooldown": 900, "env": {"XAU_SCALP_BREAKOUT_MIN_M5_ADX": "20", "XAU_SCALP_BREAKOUT_MIN_BODY_ATR": "0.80", "XAU_SCALP_BREAKOUT_MIN_VOLUME_RATIO": "1.20"}},
]


def _simulate(frame: pd.DataFrame, cfg: Any, candidate: dict[str, Any], start: datetime, end: datetime, spread: float) -> dict[str, Any]:
    keys = {"XAU_SCALP_TRIGGER_MODE": candidate["mode"], **candidate["env"]}
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    local_cfg = replace(
        cfg,
        xau_scalp_tp1_usd=float(candidate["tp1"]),
        xau_scalp_tp2_usd=float(candidate["tp2"]),
        xau_scalp_sl_usd=float(candidate["sl"]),
        xau_scalp_be_trigger_usd=float(candidate["be"]),
        xau_scalp_cooldown_seconds=int(candidate["cooldown"]),
    )
    try:
        balance = 0.0
        peak = 0.0
        max_dd = 0.0
        batches = 0
        batch_pnl: dict[int, float] = {}
        open_legs: list[dict[str, Any]] = []
        next_allowed = _ts(start)
        start_idx = max(80, _search_time(frame, start, side="left"))
        for idx in range(start_idx, len(frame)):
            row = frame.iloc[idx]
            ts = row["time"]
            if ts > _ts(end):
                break
            high, low = float(row["high"]), float(row["low"])
            survivors: list[dict[str, Any]] = []
            for leg in open_legs:
                side = leg["side"]
                advance = high - leg["entry"] if side == "buy" else leg["entry"] - low
                if leg["runner"] and advance >= float(candidate["be"]):
                    leg["sl"] = max(leg["sl"], leg["entry"] + 0.15) if side == "buy" else min(leg["sl"], leg["entry"] - 0.15)
                sl_hit = low <= leg["sl"] if side == "buy" else high >= leg["sl"]
                tp_hit = high >= leg["tp"] if side == "buy" else low <= leg["tp"]
                exit_price = leg["sl"] if sl_hit else (leg["tp"] if tp_hit else None)
                if exit_price is None:
                    survivors.append(leg)
                    continue
                move = exit_price - leg["entry"] if side == "buy" else leg["entry"] - exit_price
                pnl = move - spread
                balance += pnl
                batch_pnl[leg["batch"]] = batch_pnl.get(leg["batch"], 0.0) + pnl
                peak = max(peak, balance)
                max_dd = min(max_dd, balance - peak)
            open_legs = survivors
            if open_legs or ts < next_allowed:
                continue
            signal, _diag = _signal_from_rows(frame, idx, local_cfg)
            if not signal:
                continue
            batches += 1
            next_allowed = ts + pd.Timedelta(seconds=int(candidate["cooldown"]))
            plan = str(candidate.get("plan", "tp1,tp1,tp2")).split(",")
            targets = [(signal[target], target == "tp2") for target in plan]
            open_legs = [
                {"side": signal["side"], "entry": signal["entry"], "sl": signal["sl"], "tp": float(tp), "runner": runner, "batch": batches}
                for tp, runner in targets
            ]
        closed_batches = len(batch_pnl)
        wins = sum(1 for pnl in batch_pnl.values() if pnl > 0.0)
        losses = sum(1 for pnl in batch_pnl.values() if pnl < 0.0)
        return {
            "profit_per_001_each_leg": round(balance, 2),
            "max_drawdown": round(max_dd, 2),
            "profit_to_dd": round(balance / abs(max_dd), 3) if max_dd < 0 else 0.0,
            "batches": batches,
            "closed_batches": closed_batches,
            "wins": wins,
            "losses": losses,
            "batch_win_rate_pct": round(wins / max(1, wins + losses) * 100.0, 2),
        }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=20)
    parser.add_argument("--spread-price", type=float, default=-1.0)
    parser.add_argument("--output", default="data_vantage/xau_trigger_research_60d.json")
    parser.add_argument("--names", default="", help="Comma-separated candidate names to run")
    args = parser.parse_args()
    load_dotenv(args.env, override=True)
    os.environ["XAU_SCALP_QUALITY_FILTER_ENABLED"] = "false"
    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    tick = mt5.symbol_info_tick(symbol)
    spread = max(0.0, float(tick.ask) - float(tick.bid)) if tick and args.spread_price < 0 else max(0.0, args.spread_price)
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    split = end - timedelta(days=args.test_days)
    warmup = timedelta(days=4)
    frame = _align_frames(
        _rates(symbol, "M1", start - warmup, end + timedelta(hours=4)),
        _rates(symbol, "M5", start - warmup, end + timedelta(hours=4)),
        _rates(symbol, "M15", start - warmup, end + timedelta(hours=4)),
    )
    selected_names = {part.strip() for part in args.names.split(",") if part.strip()}
    selected = [candidate for candidate in CANDIDATES if not selected_names or candidate["name"] in selected_names]
    rows = []
    for candidate in selected:
        training = _simulate(frame, cfg, candidate, start, split, spread)
        testing = _simulate(frame, cfg, candidate, split, end, spread)
        rows.append({**candidate, "training_40d": training, "testing_20d": testing})
    objective = load_analysis_objective()
    rows = rank_profit_win(
        rows,
        profit=lambda row: float(row["testing_20d"]["profit_per_001_each_leg"]),
        win_rate=lambda row: float(row["testing_20d"]["batch_win_rate_pct"]),
        objective=objective,
    )
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range": {"start": start.isoformat(), "split": split.isoformat(), "end": end.isoformat()},
        "symbol": symbol,
        "spread_price": round(spread, 5),
        "analysis_objective": objective.as_dict(),
        "method": "fixed 0.01 per leg, 3 legs TP1/TP1/TP2, conservative SL-first candle ambiguity, closed higher timeframes",
        "results": rows,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    main()
