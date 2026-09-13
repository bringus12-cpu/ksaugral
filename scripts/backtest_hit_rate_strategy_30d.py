from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from app.telegram_signal_bot import (
    ParsedSignal,
    _channel_strategy,
    _parse_signal,
    _select_live_tps,
)


TARGET_PLAN = ((1, "be"), (1, "be"), (2, "be"))
PENDING_EXPIRY_MINUTES = 60


@dataclass
class Leg:
    entry: float
    pending: bool
    order_kind: str
    target_index: int
    protect_mode: str
    plan_index: int
    status: str = "waiting"
    sl: float = 0.0
    tp1: float = 0.0
    target: float = 0.0
    trigger_idx: int = -1
    exit_idx: int = -1
    exit_price: float = 0.0
    touched_market: bool = False


def _variants(token: str) -> set[str]:
    raw = str(token).strip()
    clean = raw.lower().lstrip("@")
    out = {clean}
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if clean.startswith(prefix):
            out.add(clean[len(prefix) :].strip("/"))
    if raw.startswith("-100"):
        out.add(raw[4:])
    return {item.strip("/") for item in out if item}


def _dialog_variants(dialog: Any) -> set[str]:
    did = str(getattr(dialog, "id", "") or "")
    username = str(getattr(dialog.entity, "username", "") or "").lower()
    out = {did, did.lstrip("-"), username}
    if did.startswith("-100"):
        out.add(did[4:])
    return {item for item in out if item}


def _tf(name: str) -> int:
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}[name.upper()]


def _rates(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, _tf(timeframe), start, end)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _hit_tp(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _hit_sl(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _entry_touched(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _price_reached(side: str, price: float, level: float) -> bool:
    return price >= level if side == "buy" else price <= level


def _better_stop(side: str, current_sl: float, candidate: float) -> float:
    if current_sl <= 0:
        return float(candidate)
    return max(float(current_sl), float(candidate)) if side == "buy" else min(float(current_sl), float(candidate))


def _pending_valid(side: str, order_kind: str, entry: float, market_price: float) -> bool:
    if order_kind == "limit":
        return entry < market_price if side == "buy" else entry > market_price
    if order_kind == "stop":
        return entry > market_price if side == "buy" else entry < market_price
    return True


def _market_entry_gap_limit(symbol_name: str, spread_points: float, min_points: int) -> float:
    info = mt5.symbol_info(symbol_name)
    point = float(getattr(info, "point", 0.01) or 0.01)
    return max(point * float(min_points) * 4.0, spread_points * point * 3.0)


def _valid_sl(side: str, entry: float, sl: float) -> bool:
    return sl > 0 and ((side == "buy" and sl < entry) or (side == "sell" and sl > entry))


def _auto_sl(symbol: str, signal: ParsedSignal, entry: float, row: pd.Series, cfg: Any) -> float:
    info = mt5.symbol_info(symbol)
    point = float(getattr(info, "point", 0.01) or 0.01)
    digits = int(getattr(info, "digits", 2) or 2)
    atr = float(row.get("atr14", 0.0) or 0.0)
    offset = max(point * float(cfg.signal_sl_min_points), atr * max(1.0, float(cfg.signal_sl_atr_mult)))
    return round(entry - offset if signal.side == "buy" else entry + offset, digits)


def _optimized_sl(symbol: str, signal: ParsedSignal, entry: float, row: pd.Series, cfg: Any) -> float:
    auto = _auto_sl(symbol, signal, entry, row, cfg)
    raw = float(signal.sl or 0.0)
    if not _valid_sl(signal.side, entry, raw):
        return auto
    auto_dist = abs(entry - auto)
    raw_dist = abs(entry - raw)
    if auto_dist <= 0 or raw_dist <= max(auto_dist * 3.0, 25.0):
        return raw
    return auto


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _plan(signal: ParsedSignal, market_price: float) -> list[Leg]:
    strategy = _channel_strategy(signal)
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    default_entry = float(signal.entry or 0.0) or market_price

    remaining_tps = (
        [float(tp) for tp in signal.tps if float(tp) > market_price]
        if signal.side == "buy"
        else [float(tp) for tp in signal.tps if float(tp) < market_price]
    )
    if signal.tps and _price_reached(signal.side, market_price, float(signal.tps[0])) and remaining_tps:
        return [Leg(market_price, False, "market", 1, "be", 1)]

    use_all_entries = strategy.force_all_entries and len(entries) > 1 and not strategy.split_target_indices
    if use_all_entries:
        planned = entries[: len(TARGET_PLAN)]
        if len(planned) < len(TARGET_PLAN):
            planned.extend([planned[-1]] * (len(TARGET_PLAN) - len(planned)))
        return [Leg(float(entry), True, "limit", target, protect, idx) for entry, (target, protect), idx in zip(planned, TARGET_PLAN, range(1, 4))]

    pending = signal.order_kind in {"limit", "stop"} and default_entry > 0
    order_kind = signal.order_kind if pending else "market"
    entry = default_entry if pending else market_price
    return [Leg(float(entry), pending, order_kind, target, protect, idx) for idx, (target, protect) in enumerate(TARGET_PLAN, start=1)]


def _simulate_signal(
    signal: ParsedSignal,
    symbol: str,
    start_idx: int,
    rates: pd.DataFrame,
    cfg: Any,
    horizon_hours: int,
    touched_entry_market: bool,
) -> list[Leg]:
    start_row = rates.iloc[start_idx]
    market_price = float(start_row["close"])
    legs = _plan(signal, market_price)
    for leg in legs:
        if leg.pending and not _pending_valid(signal.side, leg.order_kind, leg.entry, market_price):
            entry_gap = abs(float(leg.entry) - market_price)
            entry_gap_limit = _market_entry_gap_limit(symbol, 0.0, int(cfg.signal_sl_min_points))
            tp1_reached = bool(signal.tps and _price_reached(signal.side, market_price, float(signal.tps[0])))
            if touched_entry_market and leg.order_kind == "limit" and entry_gap <= entry_gap_limit and not tp1_reached:
                leg.entry = market_price
                leg.pending = False
                leg.order_kind = "market"
                leg.touched_market = True
            else:
                leg.status = "skipped"
                continue
        try:
            leg.tp1, leg.target, _live_idx, _ = _select_live_tps(signal.side, leg.entry, signal.tps, leg.target_index)
        except ValueError:
            leg.status = "skipped"
            continue
        leg.sl = _optimized_sl(symbol, signal, leg.entry, start_row, cfg)
        if not _valid_sl(signal.side, leg.entry, leg.sl):
            leg.status = "skipped"
            continue
        if signal.side == "buy" and leg.target <= leg.entry:
            leg.status = "skipped"
        if signal.side == "sell" and leg.target >= leg.entry:
            leg.status = "skipped"

    end_time = rates.iloc[start_idx]["time"] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    expiry_time = rates.iloc[start_idx]["time"] + pd.Timedelta(minutes=PENDING_EXPIRY_MINUTES)
    expiry_idx = min(int(rates["time"].searchsorted(expiry_time, side="right")), len(rates))

    for idx in range(start_idx, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])

        for leg in legs:
            if leg.status != "waiting":
                continue
            if leg.pending and idx >= expiry_idx:
                leg.status = "not_triggered"
                continue
            if leg.pending and not _entry_touched(leg.entry, high, low):
                continue
            leg.status = "open"
            leg.trigger_idx = idx

        group_tp1_seen = any(leg.status in {"open", "win", "be"} and _hit_tp(signal.side, leg.tp1, high, low) for leg in legs)
        if group_tp1_seen:
            for leg in legs:
                if leg.status == "waiting":
                    leg.status = "cancelled_after_tp1"
                if leg.status == "open":
                    leg.sl = _better_stop(signal.side, leg.sl, leg.entry)

        for leg in legs:
            if leg.status != "open":
                continue
            if _hit_sl(signal.side, leg.sl, high, low):
                leg.exit_idx = idx
                leg.exit_price = leg.sl
                leg.status = "be" if abs(leg.sl - leg.entry) < 0.05 else "loss"
                continue
            if _hit_tp(signal.side, leg.target, high, low):
                leg.exit_idx = idx
                leg.exit_price = leg.target
                leg.status = "win"

    for leg in legs:
        if leg.status == "open":
            exit_idx = max(start_idx, end_idx - 1)
            leg.exit_idx = exit_idx
            leg.exit_price = float(rates.iloc[exit_idx]["close"])
            leg.status = "timeout"
    return legs


def _lot_for_net(net_profit: float, base_lot: float, step_usd: float, add_lot: float, max_lot: float) -> float:
    steps = max(0, math.floor(max(0.0, net_profit) / step_usd))
    return min(max_lot, base_lot + (steps * add_lot))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--timeframe", default="M1", choices=["M1", "M5", "M15"])
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--start-balance", type=float, default=300.0)
    parser.add_argument("--touched-entry-market", action="store_true", help="Treat just-touched invalid limit entries as market entries when TP1 is not reached yet.")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=float(args.days))
    rates = _rates(symbol, args.timeframe, start_at - timedelta(hours=6), end_at + timedelta(hours=args.horizon_hours + 2))
    if rates.empty:
        raise RuntimeError("No XAUUSD rates returned from MT5")

    wanted = [_variants(item) for item in cfg.telegram_watch_channels]
    parsed_all = 0
    gold_signals = 0
    signal_rows: list[dict[str, Any]] = []
    session_src = (cfg.data_dir / cfg.telegram_session_name).resolve()
    if not session_src.exists() and session_src.with_suffix(".session").exists():
        session_src = session_src.with_suffix(".session")
    session_copy = cfg.data_dir / f"{cfg.telegram_session_name}_backtest_copy"
    session_copy_file = session_copy.with_suffix(".session")
    if session_src.exists():
        shutil.copy2(session_src, session_copy_file)
    client = TelegramClient(str(session_copy.resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & item for item in wanted):
                continue
            channel = str(getattr(dialog.entity, "username", "") or getattr(dialog, "id", ""))
            title = str(getattr(dialog, "title", "") or channel)
            async for message in client.iter_messages(dialog.entity):
                dt = getattr(message, "date", None)
                if dt and dt < start_at:
                    break
                text = str(getattr(message, "raw_text", "") or "")
                parsed = _parse_signal(text, f"{getattr(dialog, 'id', '')}:{getattr(message, 'id', '')}", int(getattr(dialog, "id", 0) or 0), title, "", int(getattr(message, "id", 0) or 0))
                if parsed is None:
                    continue
                parsed_all += 1
                if parsed.asset != "gold":
                    continue
                gold_signals += 1
                idx = int(rates["time"].searchsorted(pd.Timestamp(dt), side="left"))
                if idx >= len(rates):
                    continue
                legs = _simulate_signal(parsed, symbol, idx, rates, cfg, int(args.horizon_hours), bool(args.touched_entry_market))
                signal_rows.append({"time": dt, "channel": title, "message_id": int(getattr(message, "id", 0) or 0), "signal": parsed, "legs": legs})
    finally:
        await client.disconnect()

    events: list[dict[str, Any]] = []
    by_channel: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    for row in signal_rows:
        for leg in row["legs"]:
            status = leg.status
            if status in {"skipped", "waiting", "not_triggered", "cancelled_after_tp1"}:
                continue
            exit_idx = leg.exit_idx if leg.exit_idx >= 0 else int(rates["time"].searchsorted(pd.Timestamp(row["time"]), side="left"))
            exit_time = rates.iloc[min(exit_idx, len(rates) - 1)]["time"].to_pydatetime()
            profit_001 = _profit(symbol, row["signal"].side, 0.01, leg.entry, leg.exit_price)
            events.append({"exit_time": exit_time, "channel": row["channel"], "message_id": row["message_id"], "status": status, "side": row["signal"].side, "entry": leg.entry, "exit": leg.exit_price, "profit_001": profit_001, "touched_market": leg.touched_market})

    events.sort(key=lambda item: item["exit_time"])
    balance = float(args.start_balance)
    peak = balance
    max_dd = 0.0
    lot_max = max(float(cfg.max_lot), float(cfg.signal_dynamic_lot_max))
    equity = []
    for event in events:
        requested = _lot_for_net(balance - float(args.start_balance), float(cfg.signal_fixed_lot), float(cfg.signal_dynamic_lot_step_usd), float(cfg.signal_dynamic_lot_add), lot_max)
        total_lot = normalize_volume(symbol, requested, float(cfg.min_lot), lot_max)
        leg_lot = normalize_volume(symbol, total_lot / 3.0, float(cfg.min_lot), lot_max)
        profit = event["profit_001"] * (leg_lot / 0.01)
        balance += profit
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
        day = event["exit_time"].astimezone(UTC).date().isoformat()
        channel = event["channel"]
        for bucket, key in ((by_day, day), (by_channel, channel)):
            row = bucket.setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "be": 0, "timeouts": 0, "touched_market": 0, "profit": 0.0})
            row["trades"] += 1
            row["wins"] += 1 if event["status"] == "win" else 0
            row["losses"] += 1 if event["status"] == "loss" else 0
            row["be"] += 1 if event["status"] == "be" else 0
            row["timeouts"] += 1 if event["status"] == "timeout" else 0
            row["touched_market"] += 1 if event.get("touched_market") else 0
            row["profit"] += profit
        equity.append({**event, "exit_time": event["exit_time"].isoformat(), "leg_lot": round(leg_lot, 2), "profit": round(profit, 2), "balance": round(balance, 2)})

    trade_count = len(events)
    wins = sum(1 for item in events if item["status"] == "win")
    losses = sum(1 for item in events if item["status"] == "loss")
    be = sum(1 for item in events if item["status"] == "be")
    profitable_days = sum(1 for item in by_day.values() if item["profit"] > 0)
    active_days = len(by_day)
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "symbol": symbol,
        "timeframe": args.timeframe,
        "strategy": "hit-rate first: TP1/TP1/TP2, signal SL preferred, 60m pending expiry, cancel remaining pending and move open legs to BE after group TP1, one-leg momentum if signal arrives after TP1",
        "touched_entry_market_enabled": bool(args.touched_entry_market),
        "parsed_signals_all_assets": parsed_all,
        "gold_signals": gold_signals,
        "executed_or_closed_trades": trade_count,
        "wins": wins,
        "losses": losses,
        "break_even": be,
        "touched_entry_market_trades": sum(1 for item in events if item.get("touched_market")),
        "timeouts": sum(1 for item in events if item["status"] == "timeout"),
        "win_rate_ex_be": round(wins / max(1, wins + losses) * 100.0, 2),
        "non_loss_rate": round((wins + be) / max(1, wins + losses + be) * 100.0, 2),
        "start_balance": round(float(args.start_balance), 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - float(args.start_balance), 2),
        "max_drawdown_from_peak": round(max_dd, 2),
        "active_days": active_days,
        "profitable_days": profitable_days,
        "profitable_day_rate": round(profitable_days / max(1, active_days) * 100.0, 2),
        "avg_profit_per_active_day": round((balance - float(args.start_balance)) / max(1, active_days), 2),
        "by_channel": {
            key: {**{k: v for k, v in value.items() if k != "profit"}, "profit": round(value["profit"], 2), "win_rate_ex_be": round(value["wins"] / max(1, value["wins"] + value["losses"]) * 100.0, 2)}
            for key, value in sorted(by_channel.items(), key=lambda item: item[1]["profit"], reverse=True)
        },
        "by_day": {
            key: {**{k: v for k, v in value.items() if k != "profit"}, "profit": round(value["profit"], 2)}
            for key, value in sorted(by_day.items())
        },
        "trades": equity,
        "last_trades": equity[-50:],
    }
    path = Path(args.output) if args.output else cfg.data_dir / "hit_rate_strategy_backtest_30d_m1.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
