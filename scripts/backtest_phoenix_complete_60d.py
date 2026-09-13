from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _is_phoenix_direction_runner_announcement,
    _parse_signal,
    _phoenix_entry_brain,
    _phoenix_direction_hint,
    _phoenix_levels_plausible_against_market,
    _repair_gold_hundred_digit_typo,
    _repair_tps_for_entry,
    _select_live_tps,
)
from scripts.history_cache import load_cached_rates


CHANNEL_ID = -1002864291293
CHANNEL_TITLE = "PHOENIX VIP"


@dataclass
class SignalRow:
    message_id: int
    time: datetime
    signal: Any
    start_idx: int
    market: float


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    timeframe_name = str(os.getenv("PHOENIX_BACKTEST_TIMEFRAME", "M1") or "M1").strip().upper()
    history_dir = str(os.getenv("BACKTEST_HISTORY_DIR", "") or "").strip()
    if history_dir:
        return load_cached_rates(history_dir, timeframe_name, start, end)
    timeframe = mt5.TIMEFRAME_M5 if timeframe_name == "M5" else mt5.TIMEFRAME_M1
    chunks: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=14))
        raw = mt5.copy_rates_range(symbol, timeframe, cursor, chunk_end)
        if raw is not None and len(raw) > 0:
            chunks.append(pd.DataFrame(raw))
        cursor = chunk_end
    if chunks:
        frame = pd.concat(chunks, ignore_index=True)
    else:
        raw = mt5.copy_rates_from_pos(symbol, timeframe, 0, 99_999)
        if raw is None or len(raw) == 0:
            raise RuntimeError("No M1 rates returned")
        frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    frame = frame[(frame["time"] >= start_ts) & (frame["time"] <= end_ts)]
    return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _margin(symbol: str, side: str, lot: float, price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_margin(order_type, symbol, lot, price) or 0.0)


def _hit_tp(side: str, target: float, high: float, low: float) -> bool:
    return high >= target if side == "buy" else low <= target


def _hit_sl(side: str, stop: float, high: float, low: float) -> bool:
    return low <= stop if side == "buy" else high >= stop


def _reached(side: str, price: float, target: float, tolerance: float = 0.0) -> bool:
    if side == "buy":
        return price >= target - max(0.0, tolerance)
    return price <= target + max(0.0, tolerance)


def _spread_cost(symbol: str, side: str, lot: float, entry: float, spread_price: float) -> float:
    shifted = entry + spread_price if side == "buy" else entry - spread_price
    return abs(_profit(symbol, side, lot, entry, shifted))


def _simulate_market_leg(
    *,
    rates: pd.DataFrame,
    symbol: str,
    start_idx: int,
    side: str,
    lot: float,
    target_distance: float,
    stop_distance: float,
    spread_price: float,
    source: str,
    message_id: int,
    leg: str,
    horizon_hours: float = 6.0,
) -> dict[str, Any] | None:
    idx = int(start_idx)
    if idx >= len(rates):
        return None
    entry = float(rates.iloc[idx]["open"])
    target = entry + target_distance if side == "buy" else entry - target_distance
    stop = entry - stop_distance if side == "buy" else entry + stop_distance
    end_time = rates.iloc[idx]["time"] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    status = "timeout"
    exit_idx = max(idx, end_idx - 1)
    exit_price = float(rates.iloc[exit_idx]["close"])
    for bar_idx in range(idx, end_idx):
        high = float(rates.iloc[bar_idx]["high"])
        low = float(rates.iloc[bar_idx]["low"])
        if _hit_sl(side, stop, high, low):
            status, exit_idx, exit_price = "loss", bar_idx, stop
            break
        if _hit_tp(side, target, high, low):
            status, exit_idx, exit_price = "win", bar_idx, target
            break
    gross = _profit(symbol, side, lot, entry, exit_price)
    spread = _spread_cost(symbol, side, lot, entry, spread_price)
    return {
        "source": source,
        "message_id": int(message_id),
        "leg": leg,
        "side": side,
        "lot": lot,
        "entry": round(entry, 3),
        "sl": round(stop, 3),
        "tp": round(target, 3),
        "entry_time": rates.iloc[idx]["time"].isoformat(),
        "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
        "status": status,
        "gross_pnl": round(gross, 2),
        "spread_cost": round(spread, 2),
        "pnl": round(gross - spread, 2),
    }


def _simulate_signal_leg(
    *,
    rates: pd.DataFrame,
    symbol: str,
    item: SignalRow,
    entry: float,
    target_index: int,
    protect: str,
    lot: float,
    spread_price: float,
    entry_index: int,
    pending_minutes: float = 15.0,
    stop_cap: float = 5.0,
    cancel_tp_index: int = 2,
    cancel_tolerance: float = 0.25,
    horizon_hours: float = 6.0,
    range_sl_buffer: float = 0.0,
    force_range_sl: bool = False,
) -> dict[str, Any]:
    signal = item.signal
    try:
        repaired_tps = _repair_tps_for_entry(signal.side, entry, signal.tps, max(target_index, 5))
        tp1, target, live_target_index, live_tps = _select_live_tps(signal.side, entry, repaired_tps, target_index)
    except Exception as exc:
        return {"status": "skip", "reason": f"no_live_tp:{exc}"}

    sl = float(signal.sl or 0.0)
    range_sl_used = False
    entries_for_sl = sorted(float(value) for value in signal.entries if float(value or 0.0) > 0.0)
    valid_provider_sl = (signal.side == "buy" and 0.0 < sl < entry) or (signal.side == "sell" and sl > entry)
    if range_sl_buffer > 0.0 and len(entries_for_sl) >= 2 and (force_range_sl or not valid_provider_sl):
        allowance = max(float(range_sl_buffer), (entries_for_sl[-1] - entries_for_sl[0]) * 0.25)
        sl = min(entries_for_sl) - allowance if signal.side == "buy" else max(entries_for_sl) + allowance
        range_sl_used = True
    elif signal.side == "buy" and sl >= entry:
        sl = entry - stop_cap
    elif signal.side == "sell" and sl <= entry:
        sl = entry + stop_cap
    if abs(entry - sl) > stop_cap and not range_sl_used:
        sl = entry - stop_cap if signal.side == "buy" else entry + stop_cap

    start_idx = int(item.start_idx)
    expiry = rates.iloc[start_idx]["time"] + pd.Timedelta(minutes=pending_minutes)
    expiry_idx = min(int(rates["time"].searchsorted(expiry, side="right")), len(rates))
    trigger_idx = -1
    cancel_target = repaired_tps[min(cancel_tp_index - 1, len(repaired_tps) - 1)]
    for idx in range(start_idx, expiry_idx):
        row = rates.iloc[idx]
        high, low = float(row["high"]), float(row["low"])
        favorable = high if signal.side == "buy" else low
        if _reached(signal.side, favorable, cancel_target, cancel_tolerance):
            return {"status": "skip", "reason": "cancelled_near_tp2"}
        if low <= entry <= high:
            trigger_idx = idx
            break
    if trigger_idx < 0:
        return {"status": "skip", "reason": "not_triggered_15m"}

    end_time = rates.iloc[trigger_idx]["time"] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    current_sl = sl
    reached_tp1 = False
    status = "timeout"
    exit_idx = max(trigger_idx, end_idx - 1)
    exit_price = float(rates.iloc[exit_idx]["close"])
    for idx in range(trigger_idx, end_idx):
        row = rates.iloc[idx]
        high, low = float(row["high"]), float(row["low"])
        if _hit_sl(signal.side, current_sl, high, low):
            raw_pnl = _profit(symbol, signal.side, lot, entry, current_sl)
            status = "protected" if raw_pnl >= -0.01 else "loss"
            exit_idx, exit_price = idx, current_sl
            break
        if _hit_tp(signal.side, target, high, low):
            status, exit_idx, exit_price = "win", idx, target
            break
        if not reached_tp1 and _hit_tp(signal.side, tp1, high, low):
            reached_tp1 = True
            if protect == "be":
                current_sl = max(current_sl, entry) if signal.side == "buy" else min(current_sl, entry)

    gross = _profit(symbol, signal.side, lot, entry, exit_price)
    spread = _spread_cost(symbol, signal.side, lot, entry, spread_price)
    return {
        "source": "matrix_3x3",
        "message_id": int(item.message_id),
        "leg": f"E{entry_index}_TP{target_index}",
        "side": signal.side,
        "lot": lot,
        "entry": round(entry, 3),
        "sl": round(sl, 3),
        "tp": round(target, 3),
        "entry_time": rates.iloc[trigger_idx]["time"].isoformat(),
        "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
        "status": status,
        "gross_pnl": round(gross, 2),
        "spread_cost": round(spread, 2),
        "pnl": round(gross - spread, 2),
    }


def _simulate_tp1_runner(
    *,
    rates: pd.DataFrame,
    symbol: str,
    item: SignalRow,
    lot: float,
    spread_price: float,
    stop_cap: float = 5.0,
    min_reward_floor: float = 0.50,
) -> dict[str, Any]:
    idx = int(item.start_idx)
    signal = item.signal
    entry = float(rates.iloc[idx]["open"])
    live_tps = [
        float(tp) for tp in signal.tps
        if (float(tp) > entry if signal.side == "buy" else float(tp) < entry)
    ]
    if not live_tps:
        return {"status": "skip", "reason": "tp1_already_passed"}
    min_reward = max(float(min_reward_floor), spread_price * 2.0)
    target = next((tp for tp in live_tps if abs(tp - entry) >= min_reward), live_tps[-1])
    sl = float(signal.sl or 0.0)
    if signal.side == "buy" and (sl <= 0 or sl >= entry):
        sl = entry - stop_cap
    elif signal.side == "sell" and (sl <= entry):
        sl = entry + stop_cap
    if abs(entry - sl) > stop_cap:
        sl = entry - stop_cap if signal.side == "buy" else entry + stop_cap
    end_time = rates.iloc[idx]["time"] + pd.Timedelta(hours=6)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    status = "timeout"
    exit_idx = max(idx, end_idx - 1)
    exit_price = float(rates.iloc[exit_idx]["close"])
    for bar_idx in range(idx, end_idx):
        high, low = float(rates.iloc[bar_idx]["high"]), float(rates.iloc[bar_idx]["low"])
        if _hit_sl(signal.side, sl, high, low):
            status, exit_idx, exit_price = "loss", bar_idx, sl
            break
        if _hit_tp(signal.side, target, high, low):
            status, exit_idx, exit_price = "win", bar_idx, target
            break
    gross = _profit(symbol, signal.side, lot, entry, exit_price)
    spread = _spread_cost(symbol, signal.side, lot, entry, spread_price)
    return {
        "source": "tp1_runner",
        "message_id": int(item.message_id),
        "leg": "TP1_RUNNER",
        "side": signal.side,
        "lot": lot,
        "entry": round(entry, 3),
        "sl": round(sl, 3),
        "tp": round(target, 3),
        "entry_time": rates.iloc[idx]["time"].isoformat(),
        "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
        "status": status,
        "gross_pnl": round(gross, 2),
        "spread_cost": round(spread, 2),
        "pnl": round(gross - spread, 2),
    }


async def _fetch_history(rates: pd.DataFrame, cutoff: datetime, session: Path) -> tuple[list[SignalRow], list[dict[str, Any]]]:
    cfg = load_settings()
    client = TelegramClient(str(session.resolve()), int(cfg.telegram_api_id), cfg.telegram_api_hash)
    raw_messages: list[dict[str, Any]] = []
    cache_name = os.getenv("PHOENIX_RESEARCH_MESSAGE_CACHE", "")
    cache = Path(cache_name) if cache_name else None
    if cache and cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if datetime.fromisoformat(saved["cutoff"]) > cutoff:
            raise ValueError("Research message cache does not cover requested start")
        raw_messages = [{**row, "time": datetime.fromisoformat(row["time"])} for row in saved["messages"]]
    else:
        async with client:
            async for msg in client.iter_messages(CHANNEL_ID, offset_date=datetime.now(UTC), reverse=False):
                dt = msg.date.astimezone(UTC)
                if dt < cutoff:
                    break
                raw_messages.append({"id": int(msg.id), "time": dt, "text": str(msg.message or "")})
        if cache:
            cache.write_text(json.dumps({"cutoff": cutoff.isoformat(), "fetched_at": datetime.now(UTC).isoformat(), "messages": [{**row, "time": row["time"].isoformat()} for row in raw_messages]}), encoding="utf-8")

    signals: list[SignalRow] = []
    announcements: list[dict[str, Any]] = []
    side_hint: str | None = None
    side_hint_time: datetime | None = None
    broker_offset = timedelta(
        hours=float(os.getenv("PHOENIX_BACKTEST_BROKER_OFFSET_HOURS", "0") or 0.0)
    )
    for row in sorted(raw_messages, key=lambda value: value["time"]):
        text = row["text"]
        if _is_phoenix_direction_runner_announcement(text):
            side_hint = _phoenix_direction_hint(text)
            side_hint_time = row["time"]
            announcements.append({"message_id": row["id"], "time": row["time"], "side": side_hint, "text": text})
            continue
        fresh_hint = side_hint if side_hint_time and row["time"] - side_hint_time <= timedelta(minutes=20) else None
        signal = _parse_signal(
            text,
            f"{CHANNEL_ID}:{row['id']}",
            CHANNEL_ID,
            CHANNEL_TITLE,
            "",
            row["id"],
            side_hint=fresh_hint,
        )
        if signal is None or signal.asset != "gold" or not signal.entries or not signal.tps:
            continue
        broker_time = row["time"] + broker_offset
        idx = int(rates["time"].searchsorted(pd.Timestamp(broker_time).ceil("min"), side="left"))
        if idx >= len(rates):
            continue
        market = float(rates.iloc[idx]["open"])
        signal = _repair_gold_hundred_digit_typo(signal, market)
        if not _phoenix_levels_plausible_against_market(signal, market):
            continue
        signals.append(SignalRow(row["id"], row["time"], signal, idx, market))
    return signals, announcements


def _source_summary(trades: list[dict[str, Any]], source: str) -> dict[str, Any]:
    rows = [row for row in trades if row["source"] == source]
    statuses = Counter(str(row["status"]) for row in rows)
    wins = statuses["win"] + statuses["protected"]
    losses = statuses["loss"]
    decisive = wins + losses
    gross_wins = sum(max(0.0, float(row["pnl"])) for row in rows)
    gross_losses = abs(sum(min(0.0, float(row["pnl"])) for row in rows))
    net_positive = sum(1 for row in rows if float(row["pnl"]) > 0.0)
    net_negative = sum(1 for row in rows if float(row["pnl"]) < 0.0)
    net_flat = len(rows) - net_positive - net_negative
    return {
        "positions": len(rows),
        "target_or_protection_successes": wins,
        "stop_losses": losses,
        "timeouts": statuses["timeout"],
        "technical_success_rate_pct": round(wins / decisive * 100.0, 2) if decisive else 0.0,
        "net_positive_positions": net_positive,
        "net_negative_positions": net_negative,
        "net_flat_positions": net_flat,
        "net_win_rate_pct": round(net_positive / max(1, net_positive + net_negative) * 100.0, 2),
        "profit_factor": round(gross_wins / gross_losses, 3) if gross_losses else None,
        "pnl": round(sum(float(row["pnl"]) for row in rows), 2),
        "spread_cost": round(sum(float(row["spread_cost"]) for row in rows), 2),
    }


def _portfolio(symbol: str, rates: pd.DataFrame, trades: list[dict[str, Any]], start_balance: float) -> dict[str, Any]:
    events: list[tuple[pd.Timestamp, int, int, dict[str, Any]]] = []
    for trade_id, trade in enumerate(trades):
        events.append((pd.Timestamp(trade["entry_time"]), 0, trade_id, trade))
        events.append((pd.Timestamp(trade["exit_time"]), 1, trade_id, trade))
    events.sort(key=lambda item: (item[0], item[1]))
    open_trades: dict[int, dict[str, Any]] = {}
    balance = float(start_balance)
    max_open = 0
    max_open_lot = 0.0
    max_used_margin = 0.0
    margin_breaches = 0
    first_margin_breach = None
    min_free_margin = start_balance
    for timestamp, event_type, trade_id, trade in events:
        idx = int(rates["time"].searchsorted(timestamp, side="right")) - 1
        price = float(rates.iloc[max(0, min(idx, len(rates) - 1))]["close"])
        if event_type == 1:
            if open_trades.pop(trade_id, None) is not None:
                balance += float(trade["pnl"])
            continue
        floating = sum(
            _profit(symbol, row["side"], float(row["lot"]), float(row["entry"]), price)
            for row in open_trades.values()
        )
        equity = balance + floating
        used_margin = sum(float(row["margin"]) for row in open_trades.values())
        required = _margin(symbol, trade["side"], float(trade["lot"]), float(trade["entry"]))
        free_margin = equity - used_margin
        if required > free_margin:
            margin_breaches += 1
            if first_margin_breach is None:
                first_margin_breach = {
                    "time": timestamp.isoformat(),
                    "source": trade["source"],
                    "message_id": trade["message_id"],
                    "required_margin": round(required, 2),
                    "free_margin": round(free_margin, 2),
                    "balance": round(balance, 2),
                    "equity": round(equity, 2),
                }
        open_trades[trade_id] = {**trade, "margin": required}
        used_margin += required
        max_open = max(max_open, len(open_trades))
        max_open_lot = max(max_open_lot, sum(float(row["lot"]) for row in open_trades.values()))
        max_used_margin = max(max_used_margin, used_margin)
        min_free_margin = min(min_free_margin, equity - used_margin)
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "net_profit": round(balance - start_balance, 2),
        "max_simultaneous_positions": max_open,
        "max_simultaneous_lot": round(max_open_lot, 2),
        "max_used_margin": round(max_used_margin, 2),
        "min_free_margin": round(min_free_margin, 2),
        "margin_breach_events": margin_breaches,
        "first_margin_breach": first_margin_breach,
        "margin_policy": "breaches recorded; every theoretical trade retained and simulation continued",
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[".env.vantage", ".env.vantage.signal"])
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--trading-days", action="store_true",
                        help="use the last N weekdays instead of N calendar days")
    parser.add_argument("--matrix-lot", type=float, default=0.10)
    parser.add_argument("--direction-lot", type=float, default=0.10)
    parser.add_argument("--direction-targets", default="1,2,3")
    parser.add_argument("--direction-stop", type=float, default=6.0)
    parser.add_argument("--matrix-stop-cap", type=float, default=5.0)
    parser.add_argument("--runner-lot", type=float, default=1.0)
    parser.add_argument("--runner-stop-cap", type=float, default=5.0)
    parser.add_argument("--runner-min-reward", type=float, default=0.5)
    parser.add_argument("--skip-tp1-runner", action="store_true")
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument(
        "--range-layout",
        choices=["matrix3x3", "staged3"],
        default="matrix3x3",
        help="matrix3x3 opens TP1/TP2/TP5 at every range level; staged3 opens one target per level",
    )
    parser.add_argument(
        "--range-entry-policy",
        choices=["always", "respect_brain"],
        default="always",
        help="always stages range pending orders; respect_brain skips signals already past the configured TP level",
    )
    parser.add_argument(
        "--range-sl-buffer",
        type=float,
        default=0.0,
        help="derive SL beyond entry range when provider omits SL; 0 keeps legacy cap",
    )
    parser.add_argument("--force-range-sl", action="store_true",
                        help="use range geometry even when provider also supplied an SL")
    parser.add_argument("--output", default="data_vantage/backtest_phoenix_complete_60d_3x3_dir3_runner1.json")
    args = parser.parse_args()

    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        if args.trading_days:
            calendar_lookback = max(int(args.days * 1.75), int(args.days) + 20)
            probe_start = end - timedelta(days=calendar_lookback)
            probe = _rates(symbol, probe_start, end + timedelta(hours=1))
            weekdays = sorted({ts.date() for ts in probe["time"] if int(ts.weekday()) < 5})
            if len(weekdays) < int(args.days):
                raise RuntimeError(f"Only {len(weekdays)} weekdays available, need {args.days}")
            cutoff = datetime.combine(weekdays[-int(args.days)], datetime.min.time(), tzinfo=UTC)
            rates = probe
        else:
            cutoff = end - timedelta(days=int(args.days))
            rates = _rates(symbol, cutoff - timedelta(days=1), end + timedelta(hours=1))
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        spread_price = float(rates["spread"].median()) * point
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, announcements = await _fetch_history(rates, cutoff, session)

        trades: list[dict[str, Any]] = []
        skips: Counter[str] = Counter()
        full_range_signals = 0
        for item in signals:
            entries = [float(value) for value in item.signal.entries if float(value or 0.0) > 0]
            if len(entries) >= 2:
                full_range_signals += 1
                if args.range_entry_policy == "respect_brain":
                    brain = _phoenix_entry_brain(item.signal, item.market, entries)
                    if brain.get("decision") in {"skip_too_late_after_tp", "skip_not_enough_live_tps"}:
                        skips[str(brain.get("decision"))] += 1
                        continue
                entries = entries[:3]
                if len(entries) == 2:
                    low, high = min(entries), max(entries)
                    entries = [low, round((low + high) / 2.0, 3), high]
                target_rows = (
                    [(entry, target, protect) for entry in entries for target, protect in ((1, "none"), (2, "be"), (5, "be"))]
                    if args.range_layout == "matrix3x3"
                    else list(zip(entries, (1, 2, 5), ("none", "be", "be")))
                )
                for row_index, (entry, target, protect) in enumerate(target_rows, start=1):
                    entry_index = ((row_index - 1) // 3) + 1 if args.range_layout == "matrix3x3" else row_index
                    result = _simulate_signal_leg(
                        rates=rates,
                        symbol=symbol,
                        item=item,
                        entry=entry,
                        target_index=target,
                        protect=protect,
                        lot=float(args.matrix_lot),
                        spread_price=spread_price,
                        entry_index=entry_index,
                        range_sl_buffer=float(args.range_sl_buffer),
                        force_range_sl=bool(args.force_range_sl),
                        stop_cap=float(args.matrix_stop_cap),
                    )
                    if result.get("status") == "skip":
                        skips[str(result.get("reason", "skip"))] += 1
                    else:
                        trades.append(result)
            if not args.skip_tp1_runner:
                runner = _simulate_tp1_runner(
                    rates=rates,
                    symbol=symbol,
                    item=item,
                    lot=float(args.runner_lot),
                    spread_price=spread_price,
                    stop_cap=max(0.1, float(args.runner_stop_cap)),
                    min_reward_floor=max(0.1, float(args.runner_min_reward)),
                )
                if runner.get("status") == "skip":
                    skips[str(runner.get("reason", "runner_skip"))] += 1
                else:
                    trades.append(runner)

        direction_targets = [
            max(0.1, float(value.strip()))
            for value in str(args.direction_targets).split(",")
            if value.strip()
        ] or [1.0, 2.0, 3.0]
        for announcement in announcements:
            idx = int(rates["time"].searchsorted(pd.Timestamp(announcement["time"]).ceil("min"), side="left"))
            for leg_index, target_distance in enumerate(direction_targets, start=1):
                trade = _simulate_market_leg(
                    rates=rates,
                    symbol=symbol,
                    start_idx=idx,
                    side=str(announcement["side"]),
                    lot=float(args.direction_lot),
                    target_distance=target_distance,
                    stop_distance=max(0.1, float(args.direction_stop)),
                    spread_price=spread_price,
                    source="direction_3leg",
                    message_id=int(announcement["message_id"]),
                    leg=f"DIR_TP{leg_index}",
                )
                if trade is not None:
                    trades.append(trade)

        sources = {source: _source_summary(trades, source) for source in ("matrix_3x3", "direction_3leg", "tp1_runner")}
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": cutoff.isoformat(), "end": end.isoformat(), "days": int(args.days)},
            "symbol": symbol,
            "method": "M1; next bar; SL before TP on ambiguous candle; median live-terminal spread deducted",
            "settings": {
                "matrix": (
                    "3 entries x TP1/TP2/TP5"
                    if args.range_layout == "matrix3x3"
                    else "3 staged entries mapped to TP1/TP2/TP5"
                ) + ", BE on TP2/TP5 legs after TP1, SL cap 5 USD, pending 15m, cancel near TP2",
                "range_layout": args.range_layout,
                "range_entry_policy": args.range_entry_policy,
                "range_sl_buffer": float(args.range_sl_buffer),
                "force_range_sl": bool(args.force_range_sl),
                "matrix_lot_per_position": float(args.matrix_lot),
                "matrix_stop_cap": float(args.matrix_stop_cap),
                "direction": (
                    f"{len(direction_targets)} market leg(s) at announcement: "
                    f"TP distances {direction_targets} USD, SL {float(args.direction_stop):.2f} USD"
                ),
                "direction_lot_per_position": float(args.direction_lot),
                "tp1_runner_lot": float(args.runner_lot),
                "tp1_runner_stop_cap": float(args.runner_stop_cap),
                "tp1_runner_min_reward": float(args.runner_min_reward),
                "pending_cancel_tp_tolerance_usd": 0.25,
                "spread_price_median": round(spread_price, 4),
            },
            "history": {
                "parsed_full_signals": len(signals),
                "full_range_signals": full_range_signals,
                "direction_announcements": len(announcements),
                "executed_positions": len(trades),
                "skips": dict(skips),
            },
            "sources": sources,
            "combined": _source_summary(trades, "matrix_3x3"),
            "portfolio": _portfolio(symbol, rates, trades, float(args.start_balance)),
            "trades": sorted(trades, key=lambda row: (row["entry_time"], row["source"], row["leg"])),
        }
        output["combined"] = {
            "positions": len(trades),
            "target_or_protection_successes": sum(1 for row in trades if row["status"] in {"win", "protected"}),
            "stop_losses": sum(1 for row in trades if row["status"] == "loss"),
            "timeouts": sum(1 for row in trades if row["status"] == "timeout"),
            "technical_success_rate_pct": round(
                sum(1 for row in trades if row["status"] in {"win", "protected"})
                / max(1, sum(1 for row in trades if row["status"] in {"win", "protected", "loss"}))
                * 100.0,
                2,
            ),
            "net_positive_positions": sum(1 for row in trades if float(row["pnl"]) > 0.0),
            "net_negative_positions": sum(1 for row in trades if float(row["pnl"]) < 0.0),
            "net_win_rate_pct": round(
                sum(1 for row in trades if float(row["pnl"]) > 0.0)
                / max(1, sum(1 for row in trades if float(row["pnl"]) != 0.0))
                * 100.0,
                2,
            ),
            "pnl": round(sum(float(row["pnl"]) for row in trades), 2),
            "spread_cost": round(sum(float(row["spread_cost"]) for row in trades), 2),
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"history": output["history"], "sources": sources, "combined": output["combined"], "portfolio": output["portfolio"]}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
