from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _is_phoenix_direction_runner_announcement,
    _phoenix_confirmed_range_legs,
    _phoenix_direction_hint,
    _phoenix_limit_pending_levels,
    _phoenix_range_pending_levels,
)
from scripts.backtest_phoenix_complete_60d import CHANNEL_ID, _rates


RANGE_RE = re.compile(r"^\s*(\d{3,5}(?:[.,]\d+)?)\s*[/\-]\s*(\d{3,5}(?:[.,]\d+)?)\s*$")


def _completed_sessions(rates: pd.DataFrame, end: datetime, requested: int) -> tuple[list[str], datetime]:
    end_ts = pd.Timestamp(end)
    session_key = (rates["time"] + pd.Timedelta(hours=2)).dt.date
    counts = rates.loc[rates["time"] < end_ts].groupby(session_key).size()
    completed = [day for day, count in counts.items() if int(count) >= 180]
    if len(completed) < requested:
        raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {requested}")
    selected = completed[-requested:]
    mask = session_key.isin(selected)
    return [day.isoformat() for day in selected], rates.loc[mask, "time"].iloc[0].to_pydatetime()


async def _ranges(cfg: Any, cutoff: datetime, end: datetime, broker_offset: timedelta) -> list[dict[str, Any]]:
    session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
    client = TelegramClient(str(session.resolve()), int(cfg.telegram_api_id), cfg.telegram_api_hash)
    telegram_cutoff = cutoff - broker_offset - timedelta(minutes=20)
    messages: list[dict[str, Any]] = []
    async with client:
        entity = await client.get_entity(CHANNEL_ID)
        async for message in client.iter_messages(entity, offset_date=end, reverse=False):
            message_time = message.date.astimezone(UTC)
            if message_time < telegram_cutoff:
                break
            messages.append(
                {
                    "id": int(message.id),
                    "time": message_time + broker_offset,
                    "text": str(message.raw_text or ""),
                }
            )

    direction: str | None = None
    direction_time: datetime | None = None
    ttl = timedelta(minutes=max(1.0, float(os.getenv("PHOENIX_DIRECTION_HINT_TTL_MINUTES", "15") or 15)))
    rows: list[dict[str, Any]] = []
    for message in sorted(messages, key=lambda item: item["time"]):
        text = str(message["text"])
        if _is_phoenix_direction_runner_announcement(text):
            direction = _phoenix_direction_hint(text)
            direction_time = message["time"]
            continue
        match = RANGE_RE.match(text.replace(",", "."))
        if not match or direction is None or direction_time is None:
            continue
        if not timedelta(0) <= message["time"] - direction_time <= ttl:
            continue
        values = sorted([float(match.group(1)), float(match.group(2))])
        rows.append(
            {
                "message_id": int(message["id"]),
                "time": message["time"],
                "side": direction,
                "low": values[0],
                "high": values[1],
            }
        )
    return [row for row in rows if cutoff <= row["time"] < end]


def _simulate_leg(
    rates: pd.DataFrame,
    row: dict[str, Any],
    order_kind: str,
    planned_entry: float,
    target_distance: float,
    sl_distance: float,
    point: float,
    profit_per_usd_001: float,
    commission_per_001: float,
    pending_minutes: int,
    be_trigger: float,
    be_buffer: float,
) -> dict[str, Any] | None:
    times = rates["time"].to_numpy(dtype="datetime64[ns]")
    start_idx = int(rates["time"].searchsorted(pd.Timestamp(row["time"]).ceil("1min"), side="left"))
    if start_idx >= len(rates):
        return None
    side = str(row["side"])
    trigger_idx = start_idx
    fill_price = float(planned_entry)
    if order_kind == "limit":
        expiry = times[start_idx] + np.timedelta64(int(pending_minutes), "m")
        expiry_idx = min(int(np.searchsorted(times, expiry, side="right")), len(rates))
        trigger_idx = -1
        for index in range(start_idx, expiry_idx):
            spread = float(rates.iloc[index]["spread"]) * point
            touched = (
                float(rates.iloc[index]["low"]) + spread <= planned_entry
                if side == "buy"
                else float(rates.iloc[index]["high"]) >= planned_entry
            )
            if touched:
                trigger_idx = index
                break
        if trigger_idx < 0:
            return None
    else:
        spread = float(rates.iloc[start_idx]["spread"]) * point
        bid = float(rates.iloc[start_idx]["open"])
        fill_price = bid + spread if side == "buy" else bid

    target = fill_price + target_distance if side == "buy" else fill_price - target_distance
    initial_sl = fill_price - sl_distance if side == "buy" else fill_price + sl_distance
    current_sl = initial_sl
    end_time = rates.iloc[trigger_idx]["time"] + pd.Timedelta(hours=6)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    exit_idx = max(trigger_idx, end_idx - 1)
    exit_price = float(rates.iloc[exit_idx]["close"])
    status = "timeout"
    for index in range(trigger_idx, end_idx):
        spread = float(rates.iloc[index]["spread"]) * point
        high = float(rates.iloc[index]["high"])
        low = float(rates.iloc[index]["low"])
        exit_high = high if side == "buy" else high + spread
        exit_low = low if side == "buy" else low + spread
        hit_sl = exit_low <= current_sl if side == "buy" else exit_high >= current_sl
        hit_tp = exit_high >= target if side == "buy" else exit_low <= target
        if hit_sl:
            status, exit_idx, exit_price = "stop", index, current_sl
            break
        if hit_tp:
            status, exit_idx, exit_price = "target", index, target
            break
        favorable = exit_high - fill_price if side == "buy" else fill_price - exit_low
        if favorable >= be_trigger:
            candidate = fill_price + be_buffer if side == "buy" else fill_price - be_buffer
            current_sl = max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)

    signed_move = exit_price - fill_price if side == "buy" else fill_price - exit_price
    pnl = signed_move * profit_per_usd_001 - commission_per_001
    return {
        "message_id": int(row["message_id"]),
        "signal_time": row["time"].isoformat(),
        "entry_time": rates.iloc[trigger_idx]["time"].isoformat(),
        "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
        "side": side,
        "fill_kind": order_kind,
        "entry": round(fill_price, 3),
        "sl": round(initial_sl, 3),
        "tp": round(target, 3),
        "status": status,
        "pnl_001": round(pnl, 4),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--today-only", action="store_true")
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--tolerance-usd", type=float)
    parser.add_argument(
        "--output",
        default="data_vantage/current_phoenix_range_active_60sessions_20260810.json",
    )
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        if args.today_only:
            telegram_midnight = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
            cutoff = telegram_midnight + broker_offset
            end = datetime.now(UTC) + broker_offset
            rates = _rates(symbol, cutoff - timedelta(hours=1), end + timedelta(minutes=2))
            session_dates = [telegram_midnight.date().isoformat()]
        else:
            end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
            rates = _rates(symbol, end - timedelta(days=max(125, int(args.sessions * 2.0))), end + timedelta(hours=1))
            session_dates, cutoff = _completed_sessions(rates, end, int(args.sessions))
        rates = rates[(rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))].reset_index(drop=True)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        ranges = await _ranges(cfg, cutoff, end, broker_offset)
        target_distances = [
            max(0.1, float(value.strip()))
            for value in str(os.getenv("PHOENIX_RANGE_TRIGGER_TPS_USD", "1.25,2,5") or "1.25,2,5").split(",")
            if value.strip()
        ]
        sl_distance = max(max(target_distances), float(os.getenv("PHOENIX_RANGE_TRIGGER_SL_USD", "6") or 6))
        tolerance = max(
            0.0,
            float(
                args.tolerance_usd
                if args.tolerance_usd is not None
                else os.getenv("PHOENIX_RANGE_TRIGGER_TOLERANCE_USD", "0.25") or 0.25
            ),
        )
        pending_minutes = int(float(os.getenv("PHOENIX_RANGE_PENDING_EXPIRY_MINUTES", "15") or 15))
        max_pending = int(float(os.getenv("PHOENIX_RANGE_MAX_PENDING_LEGS", "2") or 2))
        be_trigger = max(0.0, float(os.getenv("PHOENIX_RANGE_TRIGGER_BE_TRIGGER_USD", "0.30") or 0.30))
        be_buffer = max(0.0, float(os.getenv("PHOENIX_RANGE_TRIGGER_BE_BUFFER_USD", "0.10") or 0.10))
        trades: list[dict[str, Any]] = []
        skipped: Counter[str] = Counter()
        for row in ranges:
            start_idx = int(rates["time"].searchsorted(pd.Timestamp(row["time"]).ceil("1min"), side="left"))
            if start_idx >= len(rates):
                skipped["no_market_bar"] += 1
                continue
            spread = float(rates.iloc[start_idx]["spread"]) * point
            bid = float(rates.iloc[start_idx]["open"])
            market = bid + spread if row["side"] == "buy" else bid
            market_usable = market <= row["high"] + tolerance if row["side"] == "buy" else market >= row["low"] - tolerance
            pending = _phoenix_range_pending_levels(row["side"], [row["low"], row["high"]], market, 0.01)
            pending = _phoenix_limit_pending_levels(row["side"], pending, max_pending)
            planned = _phoenix_confirmed_range_legs(market_usable, market, pending, len(target_distances), False)
            if not planned:
                skipped["no_executable_leg"] += 1
                continue
            for index, (kind, entry) in enumerate(planned):
                target_distance = target_distances[min(index, len(target_distances) - 1)]
                trade = _simulate_leg(
                    rates,
                    row,
                    kind,
                    float(entry),
                    target_distance,
                    sl_distance,
                    point,
                    profit_per_usd_001,
                    float(args.commission_per_001),
                    pending_minutes,
                    be_trigger,
                    be_buffer,
                )
                if trade is None:
                    skipped["pending_not_filled"] += 1
                else:
                    trade["leg"] = index + 1
                    trade["target_distance"] = target_distance
                    trades.append(trade)

        pnl = sum(float(row["pnl_001"]) for row in trades)
        wins = sum(float(row["pnl_001"]) > 0.0 for row in trades)
        losses = sum(float(row["pnl_001"]) < 0.0 for row in trades)
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions_included": session_dates,
            "today_only": bool(args.today_only),
            "symbol": symbol,
            "method": (
                "Active Phoenix preliminary direction+range replay on broker M1; one market leg when usable plus up to "
                "two deepest 15-minute limits, targets 1.25/2/5 USD, SL 6 USD and BE +0.10 after +0.30; "
                "spread and commission included; SL-first same-bar ordering"
            ),
            "ranges": len(ranges),
            "config": {
                "target_distances_usd": target_distances,
                "sl_distance_usd": sl_distance,
                "tolerance_usd": tolerance,
                "pending_minutes": pending_minutes,
                "max_pending_legs": max_pending,
                "be_trigger_usd": be_trigger,
                "be_buffer_usd": be_buffer,
            },
            "summary": {
                "positions": len(trades),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
                "pnl_001": round(pnl, 2),
                "skips": dict(skipped),
            },
            "trades": sorted(trades, key=lambda row: (row["entry_time"], row["leg"])),
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(out), "range": output["range_utc"], "summary": output["summary"]}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
