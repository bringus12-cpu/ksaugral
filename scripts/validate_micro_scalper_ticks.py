from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_xau_scalper_current import _align_frames, _rates, _search_time, _signal_from_rows


def _triggers(frame: pd.DataFrame, cfg, mode: str, start: datetime, end: datetime) -> list[dict]:
    old = os.environ.get("XAU_SCALP_TRIGGER_MODE")
    os.environ["XAU_SCALP_TRIGGER_MODE"] = mode
    local = replace(cfg, xau_scalp_tp1_usd=2.0, xau_scalp_tp2_usd=3.5, xau_scalp_sl_usd=4.0)
    result: list[dict] = []
    try:
        first = max(100, _search_time(frame, start, side="left"))
        last = min(len(frame) - 1, _search_time(frame, end, side="right"))
        for idx in range(first, last):
            signal, _ = _signal_from_rows(frame, idx, local)
            if signal:
                result.append({"time": frame.iloc[idx]["time"] + pd.Timedelta(minutes=1), "side": signal["side"]})
    finally:
        if old is None:
            os.environ.pop("XAU_SCALP_TRIGGER_MODE", None)
        else:
            os.environ["XAU_SCALP_TRIGGER_MODE"] = old
    return result


def _ticks(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=1))
        raw = mt5.copy_ticks_range(symbol, cursor, chunk_end, mt5.COPY_TICKS_ALL)
        if raw is not None and len(raw):
            chunks.append(pd.DataFrame(raw))
        cursor = chunk_end
    if not chunks:
        raise RuntimeError("No ticks returned")
    out = pd.concat(chunks, ignore_index=True).drop_duplicates("time_msc").sort_values("time_msc")
    out["time"] = pd.to_datetime(out["time_msc"], unit="ms", utc=True)
    return out[["time", "bid", "ask"]].query("bid > 0 and ask > 0").reset_index(drop=True)


def _simulate(ticks: pd.DataFrame, triggers: list[dict], cfg: dict) -> dict:
    target, stop = float(cfg["target_price"]), float(cfg["stop_price"])
    be_at = float(cfg["be_at_price"])
    cooldown = float(cfg["cooldown_seconds"])
    maximum = int(cfg["max_concurrent"])
    positions: list[dict] = []
    values: list[float] = []
    trigger_idx = 0
    next_allowed = pd.Timestamp.min.tz_localize("UTC")
    for tick in ticks.itertuples(index=False):
        bid, ask, now = float(tick.bid), float(tick.ask), tick.time
        survivors: list[dict] = []
        for position in positions:
            if position["side"] == "buy":
                favorable = bid - position["entry"]
                if be_at > 0 and favorable >= be_at:
                    position["sl"] = max(position["sl"], position["entry"] + 0.05)
                exit_price = position["sl"] if bid <= position["sl"] else position["tp"] if bid >= position["tp"] else None
                value = None if exit_price is None else exit_price - position["entry"]
            else:
                favorable = position["entry"] - ask
                if be_at > 0 and favorable >= be_at:
                    position["sl"] = min(position["sl"], position["entry"] - 0.05)
                exit_price = position["sl"] if ask >= position["sl"] else position["tp"] if ask <= position["tp"] else None
                value = None if exit_price is None else position["entry"] - exit_price
            if value is None:
                survivors.append(position)
            else:
                values.append(float(value))
        positions = survivors

        while trigger_idx < len(triggers) and triggers[trigger_idx]["time"] <= now:
            trigger = triggers[trigger_idx]
            trigger_idx += 1
            if now < next_allowed or len(positions) >= maximum:
                continue
            side = str(trigger["side"])
            entry = ask if side == "buy" else bid
            direction = 1 if side == "buy" else -1
            positions.append({"side": side, "entry": entry, "sl": entry - direction * stop,
                              "tp": entry + direction * target})
            next_allowed = now + pd.Timedelta(seconds=cooldown)

    if len(ticks):
        bid, ask = float(ticks.iloc[-1]["bid"]), float(ticks.iloc[-1]["ask"])
        for position in positions:
            values.append((bid - position["entry"]) if position["side"] == "buy" else (position["entry"] - ask))
    wins = sum(value > 0 for value in values)
    losses = sum(value < 0 for value in values)
    cumulative = peak = max_dd = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return {
        "triggers": len(triggers), "closed": len(values), "wins": wins, "losses": losses,
        "win_rate_pct": round(100 * wins / max(1, wins + losses), 2),
        "pnl_001_lot": round(sum(values), 2), "max_drawdown_001_lot": round(max_dd, 2),
        "profit_to_dd": round(sum(values) / abs(max_dd), 3) if max_dd < 0 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--research", required=True)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    research = json.loads(Path(args.research).read_text(encoding="utf-8"))
    winner = dict(research["winner"])
    load_dotenv(args.env, override=True)
    os.environ["XAU_SCALP_QUALITY_FILTER_ENABLED"] = "false"
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        start = end - timedelta(days=args.days)
        warmup = timedelta(days=4)
        frame = _align_frames(
            _rates(symbol, "M1", start - warmup, end),
            _rates(symbol, "M5", start - warmup, end),
            _rates(symbol, "M15", start - warmup, end),
        )
        triggers = _triggers(frame, cfg, str(winner["mode"]), start, end)
        ticks = _ticks(symbol, start, end)
        result = _simulate(ticks, triggers, winner)
        payload = {
            "generated_utc": datetime.now(UTC).isoformat(), "symbol": symbol,
            "period": {"start": start.isoformat(), "end": end.isoformat()}, "ticks": len(ticks),
            "configuration": {key: winner[key] for key in ("mode", "target_price", "stop_price", "be_at_price", "cooldown_seconds", "max_concurrent")},
            "result": result,
            "passed": bool(result["closed"] >= 10 and result["pnl_001_lot"] > 0 and result["profit_to_dd"] >= 1.0),
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps(payload))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
