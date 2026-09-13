from __future__ import annotations

import asyncio
import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _is_phoenix_direction_runner_announcement,
    _parse_signal,
    _phoenix_direction_hint,
)


CHANNEL_ID = -1002864291293
CHANNEL_TITLE = "PHOENIX VIP"
RANGE_RE = re.compile(r"^\s*(\d{3,5}(?:[.,]\d+)?)\s*[/\-]\s*(\d{3,5}(?:[.,]\d+)?)\s*$")


from backtest_phoenix_complete_60d import _rates


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _simulate(
    rates: pd.DataFrame,
    symbol: str,
    rows: list[dict],
    tp_distance: float,
    sl_distance: float,
    spread: float,
    be_trigger: float = 0.0,
    be_buffer: float = 0.0,
) -> dict:
    trades = []
    for row in rows:
        idx = int(rates["time"].searchsorted(pd.Timestamp(row["range_time"]).ceil("1min"), side="left"))
        if idx >= len(rates):
            continue
        entry = float(rates.iloc[idx]["open"])
        low, high = float(row["low"]), float(row["high"])
        tolerance = 2.0
        if row["side"] == "buy" and entry > high + tolerance:
            continue
        if row["side"] == "sell" and entry < low - tolerance:
            continue
        tp = entry + tp_distance if row["side"] == "buy" else entry - tp_distance
        sl = entry - sl_distance if row["side"] == "buy" else entry + sl_distance
        current_sl = sl
        end_time = rates.iloc[idx]["time"] + pd.Timedelta(hours=6)
        end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
        status = "timeout"
        exit_price = float(rates.iloc[max(idx, end_idx - 1)]["close"])
        for bar_idx in range(idx, end_idx):
            bar = rates.iloc[bar_idx]
            hit_sl = float(bar["low"]) <= current_sl if row["side"] == "buy" else float(bar["high"]) >= current_sl
            hit_tp = float(bar["high"]) >= tp if row["side"] == "buy" else float(bar["low"]) <= tp
            if hit_sl:
                status, exit_price = ("loss" if current_sl == sl else "protected"), current_sl
                break
            if hit_tp:
                status, exit_price = "win", tp
                break
            hit_be_trigger = (
                float(bar["high"]) >= entry + be_trigger
                if row["side"] == "buy"
                else float(bar["low"]) <= entry - be_trigger
            )
            if be_trigger > 0 and hit_be_trigger:
                protected_sl = entry + be_buffer if row["side"] == "buy" else entry - be_buffer
                current_sl = max(current_sl, protected_sl) if row["side"] == "buy" else min(current_sl, protected_sl)
        gross = _profit(symbol, row["side"], 1.0, entry, exit_price)
        spread_cost = abs(_profit(symbol, "buy", 1.0, entry, entry + spread))
        net = gross - spread_cost
        if status == "protected":
            status = "win" if net > 0.05 else ("loss" if net < -0.05 else "be")
        trades.append({"status": status, "pnl": net})
    statuses = Counter(item["status"] for item in trades)
    pnl = sum(item["pnl"] for item in trades)
    wins = sum(max(0.0, item["pnl"]) for item in trades)
    losses = abs(sum(min(0.0, item["pnl"]) for item in trades))
    closed = statuses["win"] + statuses["loss"]
    return {
        "tp": tp_distance,
        "sl": sl_distance,
        "be_trigger": be_trigger,
        "be_buffer": be_buffer,
        "trades": len(trades),
        "wins": statuses["win"],
        "losses": statuses["loss"],
        "be": statuses["be"],
        "timeouts": statuses["timeout"],
        "win_rate_pct": round(statuses["win"] / closed * 100.0, 2) if closed else 0.0,
        "profit_factor": round(wins / losses, 3) if losses else None,
        "pnl_1lot": round(pnl, 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=126)
    parser.add_argument("--output", default="data_vantage/phoenix_range_trigger_90sessions_20260722.json")
    args = parser.parse_args()
    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    end = datetime.now(UTC)
    start = end - timedelta(days=int(args.days))
    rates = _rates(symbol, start - timedelta(days=1), end + timedelta(hours=1))
    point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
    spread = float(rates["spread"].median()) * point
    client = TelegramClient(str((cfg.data_dir / "xauusd_signal_bot_backtest_copy.session").resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    messages = []
    try:
        entity = await client.get_entity(CHANNEL_ID)
        async for message in client.iter_messages(entity):
            if message.date < start:
                break
            messages.append({"id": int(message.id), "time": message.date.astimezone(UTC), "text": str(message.raw_text or "")})
    finally:
        await client.disconnect()

    direction = None
    direction_time = None
    pending_ranges: list[dict] = []
    pairs: list[dict] = []
    for message in sorted(messages, key=lambda item: item["time"]):
        text = message["text"]
        if _is_phoenix_direction_runner_announcement(text):
            direction = _phoenix_direction_hint(text)
            direction_time = message["time"]
            continue
        match = RANGE_RE.match(text.replace(",", "."))
        if match and direction and direction_time and message["time"] - direction_time <= timedelta(minutes=10):
            values = sorted([float(match.group(1)), float(match.group(2))])
            pending_ranges.append({
                "range_message_id": message["id"],
                "range_time": message["time"],
                "side": direction,
                "low": values[0],
                "high": values[1],
            })
            continue
        signal = _parse_signal(text, f"research:{message['id']}", CHANNEL_ID, CHANNEL_TITLE, "", message["id"], side_hint=direction)
        if signal is None or not signal.tps or not signal.entries:
            continue
        candidates = [
            item for item in pending_ranges
            if item["side"] == signal.side and timedelta(0) <= message["time"] - item["range_time"] <= timedelta(minutes=15)
        ]
        if not candidates:
            continue
        chosen = max(candidates, key=lambda item: item["range_time"])
        pairs.append({**chosen, "signal_message_id": message["id"], "signal_time": message["time"]})
        pending_ranges = [item for item in pending_ranges if item is not chosen]

    grid = []
    pair_split = max(1, int(len(pairs) * 0.60))
    for tp in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
        for sl in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0):
            protection_grid = [(0.0, 0.0)]
            protection_grid.extend(
                (trigger, buffer)
                for trigger in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
                for buffer in (0.0, 0.25, 0.5)
                if buffer < trigger < tp
            )
            for be_trigger, be_buffer in protection_grid:
                train = _simulate(rates, symbol, pairs[:pair_split], tp, sl, spread, be_trigger, be_buffer)
                holdout = _simulate(rates, symbol, pairs[pair_split:], tp, sl, spread, be_trigger, be_buffer)
                full = _simulate(rates, symbol, pairs, tp, sl, spread, be_trigger, be_buffer)
                grid.append({
                    "tp": tp,
                    "sl": sl,
                    "be_trigger": be_trigger,
                    "be_buffer": be_buffer,
                    "train": train,
                    "holdout": holdout,
                    "full": full,
                })
    robust = [item for item in grid if item["train"]["pnl_1lot"] > 0 and item["holdout"]["pnl_1lot"] > 0]
    robust.sort(
        key=lambda item: (
            min(item["train"]["profit_factor"] or 0.0, item["holdout"]["profit_factor"] or 0.0),
            item["full"]["pnl_1lot"],
        ),
        reverse=True,
    )
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_start_utc": start.isoformat(),
        "method": "market at next available M1 after Phoenix numeric range; requires fresh prior direction; 2 USD range tolerance; spread deducted; conservative SL-first intrabar ordering",
        "paired_sequences": len(pairs),
        "spread": spread,
        "best": robust[0] if robust else None,
        "top10": robust[:10],
        "robust_configs": len(robust),
        "pairs": [{**item, "range_time": item["range_time"].isoformat(), "signal_time": item["signal_time"].isoformat()} for item in pairs],
    }
    out = Path(args.output)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("paired_sequences", "best", "top10")}, indent=2))
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
