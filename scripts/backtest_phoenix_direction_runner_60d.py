from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _is_phoenix_direction_runner_announcement, _phoenix_direction_hint


def _rates(symbol: str) -> pd.DataFrame:
    raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 99_999)
    if raw is None or len(raw) == 0:
        raise RuntimeError("No M1 rates returned from MT5")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[".env.vantage", ".env.vantage.signal"])
    parser.add_argument("--session-name", default="xauusd_signal_bot_vantage_discover50_reserve12")
    parser.add_argument("--start", default="2026-04-28T00:00:00+00:00")
    parser.add_argument("--tp-usd", type=float, default=1.5)
    parser.add_argument("--sl-usd", type=float, default=3.0)
    parser.add_argument("--lot", type=float, default=1.0)
    parser.add_argument("--horizon-hours", type=float, default=24.0)
    parser.add_argument("--output", default="data_vantage/backtest_phoenix_direction_runner_60sessions.json")
    args = parser.parse_args()

    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    rates = _rates(symbol)
    point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
    spread_price = float(rates["spread"].median()) * point
    start = datetime.fromisoformat(args.start).astimezone(UTC)

    client = TelegramClient(str((cfg.data_dir / args.session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    announcements = []
    try:
        entity = await client.get_entity(-1002864291293)
        async for message in client.iter_messages(entity):
            if message.date < start:
                break
            text = str(message.raw_text or "")
            if _is_phoenix_direction_runner_announcement(text):
                announcements.append(
                    {
                        "message_id": int(message.id),
                        "time": message.date.astimezone(UTC),
                        "side": _phoenix_direction_hint(text),
                        "text": text,
                    }
                )
    finally:
        await client.disconnect()

    rows = []
    for item in sorted(announcements, key=lambda row: row["time"]):
        # Enter at the next full M1 bar to avoid using pre-publication movement
        # from the Telegram message's current minute.
        idx = int(rates["time"].searchsorted(pd.Timestamp(item["time"]).ceil("min"), side="left"))
        if idx >= len(rates):
            continue
        entry = float(rates.iloc[idx]["open"])
        side = str(item["side"])
        target = entry + args.tp_usd if side == "buy" else entry - args.tp_usd
        stop = entry - args.sl_usd if side == "buy" else entry + args.sl_usd
        end_time = rates.iloc[idx]["time"] + pd.Timedelta(
            seconds=max(1, round(args.horizon_hours * 3600.0))
        )
        end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
        status = "timeout"
        exit_price = float(rates.iloc[max(idx, end_idx - 1)]["close"])
        exit_idx = max(idx, end_idx - 1)
        for bar_idx in range(idx, end_idx):
            high = float(rates.iloc[bar_idx]["high"])
            low = float(rates.iloc[bar_idx]["low"])
            hit_sl = low <= stop if side == "buy" else high >= stop
            hit_tp = high >= target if side == "buy" else low <= target
            if hit_sl:
                status, exit_price, exit_idx = "loss", stop, bar_idx
                break
            if hit_tp:
                status, exit_price, exit_idx = "win", target, bar_idx
                break
        gross = _profit(symbol, side, args.lot, entry, exit_price)
        spread_cost = abs(_profit(symbol, "buy", args.lot, entry, entry + spread_price))
        pnl = gross - spread_cost
        rows.append(
            {
                "message_id": item["message_id"],
                "signal_time": item["time"].isoformat(),
                "entry_time": rates.iloc[idx]["time"].isoformat(),
                "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
                "side": side,
                "entry": round(entry, 2),
                "sl": round(stop, 2),
                "tp": round(target, 2),
                "status": status,
                "gross": round(gross, 2),
                "spread_cost": round(spread_cost, 2),
                "pnl": round(pnl, 2),
            }
        )

    balance = 0.0
    peak = 0.0
    max_dd = 0.0
    by_month: dict[str, float] = defaultdict(float)
    by_side: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for row in rows:
        balance += float(row["pnl"])
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
        by_month[str(row["signal_time"])[:7]] += float(row["pnl"])
        side_row = by_side[str(row["side"])]
        side_row["trades"] += 1
        side_row["wins"] += int(row["status"] == "win")
        side_row["losses"] += int(row["status"] == "loss")
        side_row["pnl"] += float(row["pnl"])
    statuses = Counter(row["status"] for row in rows)
    gross_wins = sum(max(0.0, float(row["pnl"])) for row in rows)
    gross_losses = abs(sum(min(0.0, float(row["pnl"])) for row in rows))
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_start_utc": start.isoformat(),
        "range_end_utc": rates.iloc[-1]["time"].isoformat(),
        "method": "next M1 open after announcement; SL before TP on same candle; median MT5 spread deducted",
        "symbol": symbol,
        "lot": args.lot,
        "tp_usd": args.tp_usd,
        "sl_usd": args.sl_usd,
        "spread_price": round(spread_price, 4),
        "announcements": len(rows),
        "statuses": dict(statuses),
        "win_rate_pct": round(statuses["win"] / len(rows) * 100.0, 2) if rows else 0.0,
        "profit_factor": round(gross_wins / gross_losses, 3) if gross_losses else None,
        "pnl": round(balance, 2),
        "max_drawdown": round(max_dd, 2),
        "by_month": {key: round(value, 2) for key, value in by_month.items()},
        "by_side": {key: {**value, "pnl": round(value["pnl"], 2)} for key, value in by_side.items()},
        "trades": rows,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(out)
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
