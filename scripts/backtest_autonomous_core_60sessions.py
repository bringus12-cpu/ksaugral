from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown, symbol_info
from app.strategies import _enabled_autonomous_strategies


def _rates(symbol: str, timeframe: int, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, timeframe, start, end)
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"No rates for {symbol}: {mt5.last_error()}")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _candidate(m5: pd.DataFrame, index: int, m15: pd.DataFrame, regime_index: int, cfg) -> dict | None:
    one = m5.iloc[index]
    previous = m5.iloc[index - 1]
    regime = m15.iloc[regime_index]
    trend_up = regime["ema50"] > regime["ema200"] and regime["close"] > regime["ema20"]
    trend_down = regime["ema50"] < regime["ema200"] and regime["close"] < regime["ema20"]
    regime_side = "buy" if trend_up and regime["adx14"] >= 16 else "sell" if trend_down and regime["adx14"] >= 16 else "flat"
    atr = float(one["atr14"] or 0.0)
    if atr <= 0.0:
        return None
    candidates: list[dict] = []
    if regime_side == "buy" and previous["close"] <= previous["ema20"] and one["close"] > one["ema20"] and one["rsi14"] > 52 and one["adx14"] >= 16:
        candidates.append({"strategy": "trend_pullback", "side": "buy", "score": 74.0, "sl_mult": cfg.trend_sl_atr, "rr": cfg.trend_tp_rr})
    if regime_side == "sell" and previous["close"] >= previous["ema20"] and one["close"] < one["ema20"] and one["rsi14"] < 48 and one["adx14"] >= 16:
        candidates.append({"strategy": "trend_pullback", "side": "sell", "score": 74.0, "sl_mult": cfg.trend_sl_atr, "rr": cfg.trend_tp_rr})
    volume_ok = float(one["tick_volume"] or 0.0) > float(one["volume_ma20"] or 0.0) * 1.15
    if regime_side == "buy" and one["close"] > one["hh20"] and one["adx14"] >= 18 and one["rsi14"] >= 58 and volume_ok:
        candidates.append({"strategy": "breakout_momentum", "side": "buy", "score": 82.0, "sl_mult": cfg.breakout_sl_atr, "rr": cfg.breakout_tp_rr})
    if regime_side == "sell" and one["close"] < one["ll20"] and one["adx14"] >= 18 and one["rsi14"] <= 42 and volume_ok:
        candidates.append({"strategy": "breakout_momentum", "side": "sell", "score": 82.0, "sl_mult": cfg.breakout_sl_atr, "rr": cfg.breakout_tp_rr})
    range_mode = 10.0 <= float(one["adx14"] or 0.0) <= 18.0
    if regime_side == "flat" and range_mode and one["close"] <= one["bb_lower"] and one["rsi14"] <= 33:
        candidates.append({"strategy": "mean_reversion", "side": "buy", "score": 65.0, "sl_mult": cfg.meanrev_sl_atr, "rr": cfg.meanrev_tp_rr})
    if regime_side == "flat" and range_mode and one["close"] >= one["bb_upper"] and one["rsi14"] >= 67:
        candidates.append({"strategy": "mean_reversion", "side": "sell", "score": 65.0, "sl_mult": cfg.meanrev_sl_atr, "rr": cfg.meanrev_tp_rr})
    enabled = _enabled_autonomous_strategies()
    candidates = [
        row
        for row in candidates
        if row["strategy"] in enabled and row["score"] >= cfg.min_signal_score
    ]
    return max(candidates, key=lambda row: row["score"]) if candidates else None


def _profit(symbol: str, side: str, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, 0.01, entry, exit_price) or 0.0)


def _simulate(symbol: str, m5: pd.DataFrame, entry_index: int, signal: dict, cfg, point: float) -> dict:
    side = str(signal["side"])
    signal_close = float(m5.iloc[entry_index - 1]["close"])
    atr = float(m5.iloc[entry_index - 1]["atr14"])
    spread = max(point, float(m5.iloc[entry_index].get("spread", 0.0) or 0.0) * point)
    raw_open = float(m5.iloc[entry_index]["open"])
    entry = raw_open + spread if side == "buy" else raw_open
    distance = atr * float(signal["sl_mult"])
    sl = signal_close - distance if side == "buy" else signal_close + distance
    tp = signal_close + distance * float(signal["rr"]) if side == "buy" else signal_close - distance * float(signal["rr"])
    if (side == "buy" and not (sl < entry < tp)) or (side == "sell" and not (tp < entry < sl)):
        return {"status": "gap_invalid"}
    initial_sl = sl
    initial_risk = abs(entry - initial_sl)
    exit_index = len(m5) - 1
    exit_price = float(m5.iloc[-1]["close"])
    status = "end_of_test"
    for index in range(entry_index, len(m5)):
        row = m5.iloc[index]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        ask_high = high + spread
        ask_close = close + spread
        hit_sl = low <= sl if side == "buy" else ask_high >= sl
        hit_tp = high >= tp if side == "buy" else low <= tp
        if hit_sl:
            exit_index, exit_price, status = index, sl, "sl"
            break
        if hit_tp:
            exit_index, exit_price, status = index, tp, "tp"
            break
        mark = close if side == "buy" else ask_close
        favorable = mark - entry if side == "buy" else entry - mark
        r_multiple = favorable / initial_risk if initial_risk > 0 else 0.0
        if r_multiple >= cfg.breakeven_at_r:
            candidate = entry + 0.05 * atr if side == "buy" else entry - 0.05 * atr
            sl = max(sl, candidate) if side == "buy" else min(sl, candidate)
        if r_multiple >= cfg.trail_start_r:
            candidate = close - atr * cfg.trail_atr_mult if side == "buy" else ask_close + atr * cfg.trail_atr_mult
            sl = max(sl, candidate) if side == "buy" else min(sl, candidate)
    return {
        "status": status,
        "opened": pd.Timestamp(m5.iloc[entry_index]["time"]).isoformat(),
        "closed": pd.Timestamp(m5.iloc[exit_index]["time"]).isoformat(),
        "side": side,
        "strategy": signal["strategy"],
        "score": signal["score"],
        "entry": round(entry, 8),
        "initial_sl": round(initial_sl, 8),
        "tp": round(tp, 8),
        "exit": round(exit_price, 8),
        "profit_001": round(_profit(symbol, side, entry, exit_price), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Current autonomous XAU core on the latest trading sessions")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--output", default="data_vantage/fullstack_autonomous_core_60sessions.json")
    args = parser.parse_args()
    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        start = end - timedelta(days=max(120, args.sessions * 2 + 30))
        m5 = _rates(symbol, mt5.TIMEFRAME_M5, start, end)
        m15 = _rates(symbol, mt5.TIMEFRAME_M15, start, end)
        dates = list(dict.fromkeys(m5["time"].dt.date.tolist()))
        selected = set(dates[-args.sessions :])
        point = float(getattr(symbol_info(symbol), "point", 0.01) or 0.01)
        trades = []
        last_entry_index = -100000
        for index in range(220, len(m5) - 1):
            if m5.iloc[index]["time"].date() not in selected:
                continue
            if index - last_entry_index < max(1, int(cfg.cooldown_bars)):
                continue
            regime_index = int(m15["time"].searchsorted(m5.iloc[index]["time"], side="right")) - 1
            if regime_index < 220:
                continue
            signal = _candidate(m5, index, m15, regime_index, cfg)
            if not signal:
                continue
            result = _simulate(symbol, m5, index + 1, signal, cfg, point)
            if result.get("status") == "gap_invalid":
                continue
            trades.append(result)
            last_entry_index = index
    finally:
        shutdown()
    profits = [float(row["profit_001"]) for row in trades]
    wins = [value for value in profits if value > 0]
    losses = [value for value in profits if value < 0]
    gross_loss = abs(sum(losses))
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "sessions": args.sessions,
        "symbol": symbol,
        "method": "M5/M15 current autonomous signal rules; next M5 open with spread; SL-first ambiguity; candle-close BE/trailing; reversal and partial-close execution omitted.",
        "summary_at_001": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / max(1, len(wins) + len(losses)), 2),
            "pnl": round(sum(profits), 2),
            "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss else None,
        },
        "trades": trades,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "trades"}, indent=2))
    print(f"REPORT={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
