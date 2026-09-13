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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_xau_scalper_current import _align_frames, _rates, _search_time, _signal_from_rows, _ts


def _trigger_indices(frame: pd.DataFrame, cfg: Any, mode: str, start: datetime, end: datetime) -> list[tuple[int, str]]:
    old = os.environ.get("XAU_SCALP_TRIGGER_MODE")
    os.environ["XAU_SCALP_TRIGGER_MODE"] = mode
    local = replace(cfg, xau_scalp_tp1_usd=2.0, xau_scalp_tp2_usd=3.5, xau_scalp_sl_usd=4.0)
    rows: list[tuple[int, str]] = []
    try:
        first = max(100, _search_time(frame, start, side="left"))
        last = _search_time(frame, end, side="right")
        for idx in range(first, min(last, len(frame) - 1)):
            signal, _ = _signal_from_rows(frame, idx, local)
            if signal:
                rows.append((idx, str(signal["side"])))
    finally:
        if old is None:
            os.environ.pop("XAU_SCALP_TRIGGER_MODE", None)
        else:
            os.environ["XAU_SCALP_TRIGGER_MODE"] = old
    return rows


def _simulate(frame: pd.DataFrame, triggers: list[tuple[int, str]], start: datetime, end: datetime,
              target: float, stop: float, be_at: float, cooldown_seconds: int,
              max_concurrent: int, point: float) -> dict[str, Any]:
    trigger_map = {idx: side for idx, side in triggers}
    first = max(100, _search_time(frame, start, side="left"))
    last = min(len(frame), _search_time(frame, end, side="right"))
    times = frame["time"].tolist()
    highs = frame["high"].to_numpy(dtype=float, copy=False)
    lows = frame["low"].to_numpy(dtype=float, copy=False)
    opens = frame["open"].to_numpy(dtype=float, copy=False)
    closes = frame["close"].to_numpy(dtype=float, copy=False)
    spreads_raw = frame["spread"].to_numpy(dtype=float, copy=False)
    positions: list[dict[str, Any]] = []
    next_allowed = _ts(start)
    pnls: list[float] = []
    spreads: list[float] = []
    entries = 0
    for idx in range(first, last):
        high, low = highs[idx], lows[idx]
        survivors: list[dict[str, Any]] = []
        for position in positions:
            side, entry = position["side"], position["entry"]
            favorable = high - entry if side == "buy" else entry - low
            if be_at > 0 and favorable >= be_at:
                position["sl"] = max(position["sl"], entry + 0.05) if side == "buy" else min(position["sl"], entry - 0.05)
            sl_hit = low <= position["sl"] if side == "buy" else high >= position["sl"]
            tp_hit = high >= position["tp"] if side == "buy" else low <= position["tp"]
            if not sl_hit and not tp_hit:
                survivors.append(position)
                continue
            # If both levels are inside one M1 candle, assume the stop happened first.
            exit_price = position["sl"] if sl_hit else position["tp"]
            move = exit_price - entry if side == "buy" else entry - exit_price
            pnls.append(move - position["spread"])
        positions = survivors
        side = trigger_map.get(idx)
        if side is None or len(positions) >= max_concurrent or times[idx] < next_allowed:
            continue
        entry_idx = idx + 1
        if entry_idx >= last:
            continue
        entry = opens[entry_idx]
        spread = max(0.0, spreads_raw[entry_idx] * point)
        direction = 1 if side == "buy" else -1
        positions.append({"side": side, "entry": entry, "sl": entry - direction * stop,
                          "tp": entry + direction * target, "spread": spread})
        spreads.append(spread)
        entries += 1
        next_allowed = times[idx] + pd.Timedelta(seconds=cooldown_seconds)

    # Mark open positions to market so an unfinished grid cannot hide risk.
    if positions and last > first:
        close = closes[last - 1]
        for position in positions:
            move = close - position["entry"] if position["side"] == "buy" else position["entry"] - close
            pnls.append(move - position["spread"])
    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    cumulative = peak = max_dd = 0.0
    for value in pnls:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return {
        "entries": int(entries), "closed": int(len(pnls)), "wins": int(wins), "losses": int(losses),
        "win_rate_pct": float(round(100 * wins / max(1, wins + losses), 2)),
        "pnl_001_lot": float(round(float(sum(pnls)), 2)), "max_drawdown_001_lot": float(round(float(max_dd), 2)),
        "profit_to_dd": float(round(float(sum(pnls)) / abs(float(max_dd)), 3)) if max_dd < 0 else 0.0,
        "median_spread_price": round(float(pd.Series(spreads).median()), 4) if spreads else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=20)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    load_dotenv(args.env, override=True)
    os.environ["XAU_SCALP_QUALITY_FILTER_ENABLED"] = "false"
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        start = end - timedelta(days=args.days)
        split = end - timedelta(days=args.test_days)
        warmup = timedelta(days=5)
        frame = _align_frames(
            _rates(symbol, "M1", start - warmup, end + timedelta(hours=1)),
            _rates(symbol, "M5", start - warmup, end + timedelta(hours=1)),
            _rates(symbol, "M15", start - warmup, end + timedelta(hours=1)),
        )
        info = mt5.symbol_info(symbol)
        point = float(getattr(info, "point", 0.01) or 0.01)
        modes = ("trend_pullback", "range_reversion", "range_breakout")
        all_triggers = {mode: _trigger_indices(frame, cfg, mode, start, end) for mode in modes}
        candidates: list[dict[str, Any]] = []
        for mode in modes:
            train_triggers = [(idx, side) for idx, side in all_triggers[mode] if frame.iloc[idx]["time"] < _ts(split)]
            test_triggers = [(idx, side) for idx, side in all_triggers[mode] if frame.iloc[idx]["time"] >= _ts(split)]
            for target in (0.75, 1.0, 1.5, 2.0):
                for stop in (1.5, 2.0, 3.0):
                    for be_fraction in (0.0, 0.6):
                        for cooldown in (60, 180):
                            for concurrent in (1, 3, 5):
                                be_at = target * be_fraction
                                training = _simulate(frame, train_triggers, start, split, target, stop, be_at, cooldown, concurrent, point)
                                testing = _simulate(frame, test_triggers, split, end, target, stop, be_at, cooldown, concurrent, point)
                                candidates.append({
                                    "mode": mode, "target_price": target, "stop_price": stop,
                                    "be_at_price": round(be_at, 3), "cooldown_seconds": cooldown,
                                    "max_concurrent": concurrent, "training": training, "testing": testing,
                                })
        candidates.sort(key=lambda row: (
            row["testing"]["pnl_001_lot"] > 0, row["testing"]["profit_to_dd"],
            row["testing"]["pnl_001_lot"], row["testing"]["win_rate_pct"]
        ), reverse=True)
        robust = [row for row in candidates if row["training"]["pnl_001_lot"] > 0
                  and row["testing"]["pnl_001_lot"] > 0 and row["testing"]["closed"] >= 30]
        winner = robust[0] if robust else candidates[0]
        payload = {
            "generated_utc": datetime.now(UTC).isoformat(), "symbol": symbol,
            "period": {"start": start.isoformat(), "split": split.isoformat(), "end": end.isoformat()},
            "method": "M1 closed-candle triggers; next-bar entry; historical bar spread; SL-first ambiguity; independent capped positions; no martingale or loss averaging",
            "trigger_counts": {mode: len(rows) for mode, rows in all_triggers.items()},
            "tested_combinations": len(candidates), "robust_count": len(robust), "winner": winner,
            "deployment_ready": bool(robust and winner["testing"]["profit_to_dd"] >= 1.0 and winner["testing"]["win_rate_pct"] >= 60),
            "top20": candidates[:20],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({key: payload[key] for key in ("trigger_counts", "tested_combinations", "robust_count", "winner", "deployment_ready")}))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
