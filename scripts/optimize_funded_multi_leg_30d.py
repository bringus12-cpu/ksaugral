from __future__ import annotations

import argparse
import asyncio
import json
import math
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
from app.telegram_signal_bot import _parse_signal, _strict_live_tps_for_entry


@dataclass(frozen=True)
class SignalRow:
    dt: datetime
    channel: str
    message_id: int
    signal: Any
    start_idx: int


def _variants(token: str) -> set[str]:
    raw = str(token).strip()
    clean = raw.lower().lstrip("@")
    out = {clean}
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
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


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
    if raw is None or len(raw) == 0:
        raise RuntimeError("No M1 rates returned")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _hit_tp(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _hit_sl(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _valid(side: str, entry: float, sl: float, tp: float) -> bool:
    if not all(math.isfinite(value) and value > 0 for value in (entry, sl, tp)):
        return False
    if side == "buy":
        return sl < entry < tp
    return tp < entry < sl


def _cap_sl(side: str, entry: float, sl: float, max_dist: float) -> float:
    if max_dist <= 0:
        return sl
    fallback = entry - max_dist if side == "buy" else entry + max_dist
    if sl <= 0:
        return fallback
    if side == "buy" and sl >= entry:
        return fallback
    if side == "sell" and sl <= entry:
        return fallback
    if abs(entry - sl) > max_dist:
        return fallback
    return sl


def _entry_touched(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _price_reached(side: str, price: float, level: float) -> bool:
    return price >= level if side == "buy" else price <= level


def _better_stop(side: str, current_sl: float, candidate: float) -> float:
    if current_sl <= 0:
        return candidate
    return max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)


def _entries_for_mode(signal: Any, market: float, mode: str) -> list[tuple[float, bool]]:
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    if not entries:
        entries = [float(signal.entry or market)]
    low = min(entries)
    high = max(entries)
    if mode == "market_near":
        if not (low - 2.0 <= market <= high + 2.0):
            return []
        return [(market, False)]
    if mode == "market_between_entry_tp1":
        try:
            live = _strict_live_tps_for_entry(signal.side, market, signal.tps)
        except Exception:
            return []
        if not live:
            return []
        tp1 = float(live[0])
        ok = market < tp1 if signal.side == "buy" else market > tp1
        if not ok:
            return []
        return [(market, False)]
    if mode == "nearest_pending_15m":
        return [(min(entries, key=lambda value: abs(value - market)), True)]
    if mode == "all_entries_pending_15m":
        clean = sorted(set(round(value, 2) for value in entries))
        return [(value, True) for value in clean[:3]]
    if mode == "hybrid_market_near_plus_pending":
        out: list[tuple[float, bool]] = []
        if low - 1.0 <= market <= high + 1.0:
            out.append((market, False))
        nearest = min(entries, key=lambda value: abs(value - market))
        out.append((nearest, True))
        return out
    raise ValueError(mode)


def _simulate_leg(
    signal: Any,
    symbol: str,
    rates: pd.DataFrame,
    start_idx: int,
    entry: float,
    pending: bool,
    target_index: int,
    protect: str,
    sl_cap: float,
    pending_minutes: float,
    horizon_hours: float,
    stale_profit_minutes: float = 0.0,
    initial_market_price: float | None = None,
) -> dict[str, Any]:
    times = rates["time"]
    highs = rates["high"].to_numpy(copy=False)
    lows = rates["low"].to_numpy(copy=False)
    closes = rates["close"].to_numpy(copy=False)
    try:
        tp1, target, _live_idx, live_tps = _select_live(signal.side, entry, signal.tps, target_index)
    except Exception:
        return {"status": "skip", "reason": "no_live_tp"}
    sl = _cap_sl(signal.side, entry, float(signal.sl or 0.0), sl_cap)
    if not _valid(signal.side, entry, sl, tp1) or not _valid(signal.side, entry, sl, target):
        return {"status": "skip", "reason": "invalid_levels"}

    trigger_idx = start_idx
    if pending:
        start_price = float(closes[start_idx]) if initial_market_price is None else float(initial_market_price)
        if signal.side == "buy" and entry >= start_price:
            return {"status": "skip", "reason": "wrong_pending_side"}
        if signal.side == "sell" and entry <= start_price:
            return {"status": "skip", "reason": "wrong_pending_side"}
        expiry = times.iat[start_idx] + pd.Timedelta(minutes=pending_minutes)
        expiry_idx = min(int(times.searchsorted(expiry, side="right")), len(rates))
        trigger_idx = -1
        for idx in range(start_idx, expiry_idx):
            high = float(highs[idx])
            low = float(lows[idx])
            if _hit_tp(signal.side, tp1, high, low):
                return {"status": "skip", "reason": "tp_before_entry"}
            if _entry_touched(entry, high, low):
                trigger_idx = idx
                break
        if trigger_idx < 0:
            return {"status": "skip", "reason": "not_triggered"}

    end_time = times.iat[trigger_idx] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(times.searchsorted(end_time, side="right")), len(rates))
    current_sl = sl
    reached_level = 0
    last_progress_idx = trigger_idx
    for idx in range(trigger_idx, end_idx):
        high = float(highs[idx])
        low = float(lows[idx])
        if _hit_sl(signal.side, current_sl, high, low):
            status = "be" if abs(current_sl - entry) < 0.05 else "loss"
            return {"status": status, "entry": entry, "exit": current_sl, "entry_idx": trigger_idx, "exit_idx": idx}
        if _hit_tp(signal.side, target, high, low):
            return {"status": "win", "entry": entry, "exit": target, "entry_idx": trigger_idx, "exit_idx": idx}
        previous_reached_level = reached_level
        for level, level_tp in enumerate(live_tps, start=1):
            if level > reached_level and _hit_tp(signal.side, float(level_tp), high, low):
                reached_level = level
        if reached_level > previous_reached_level:
            last_progress_idx = idx
        if protect == "be" and reached_level >= 1:
            current_sl = _better_stop(signal.side, current_sl, entry)
        elif protect == "tp1" and reached_level >= 1:
            current_sl = _better_stop(signal.side, current_sl, tp1)
        elif protect == "be_after_tp2" and reached_level >= 2:
            current_sl = _better_stop(signal.side, current_sl, entry)
        elif protect == "be_after_tp3" and reached_level >= 3:
            current_sl = _better_stop(signal.side, current_sl, entry)
        elif protect == "tp1_after_tp2" and reached_level >= 2:
            current_sl = _better_stop(signal.side, current_sl, float(live_tps[0]))
        elif protect == "tp1_after_tp3" and reached_level >= 3:
            current_sl = _better_stop(signal.side, current_sl, float(live_tps[0]))
        elif protect == "progressive_ladder" and reached_level >= 1:
            candidate = entry if reached_level == 1 else float(live_tps[min(reached_level - 2, len(live_tps) - 1)])
            current_sl = _better_stop(signal.side, current_sl, candidate)
        elif protect == "delayed_ladder" and reached_level >= 3:
            candidate = float(live_tps[min(reached_level - 3, len(live_tps) - 1)])
            current_sl = _better_stop(signal.side, current_sl, candidate)
        if stale_profit_minutes > 0 and reached_level >= 1:
            stale_at = times.iat[last_progress_idx] + pd.Timedelta(minutes=float(stale_profit_minutes))
            close = float(closes[idx])
            profitable = close > entry if signal.side == "buy" else close < entry
            if times.iat[idx] >= stale_at and profitable:
                return {"status": "win", "entry": entry, "exit": close, "entry_idx": trigger_idx, "exit_idx": idx, "reason": "stale_profit_exit"}
    exit_idx = max(trigger_idx, end_idx - 1)
    return {"status": "timeout", "entry": entry, "exit": float(closes[exit_idx]), "entry_idx": trigger_idx, "exit_idx": exit_idx}


def _select_live(side: str, entry: float, tps: list[float], target_index: int) -> tuple[float, float, int, list[float]]:
    live = _strict_live_tps_for_entry(side, entry, tps)
    if not live:
        raise ValueError("no_live_tp")
    idx = min(max(1, int(target_index)), len(live))
    return float(live[0]), float(live[idx - 1]), idx, live


def _score_events(events: list[dict[str, Any]], account: float, daily_pct: float, total_pct: float) -> dict[str, Any]:
    events = sorted(events, key=lambda item: item["exit_time"])
    base_profit = sum(float(item["p001"]) for item in events)
    balance = 0.0
    peak = 0.0
    max_dd_001 = 0.0
    by_day: dict[str, float] = {}
    wins = losses = be = timeouts = 0
    for item in events:
        pnl = float(item["p001"])
        balance += pnl
        peak = max(peak, balance)
        max_dd_001 = min(max_dd_001, balance - peak)
        day = item["exit_time"][:10]
        by_day[day] = by_day.get(day, 0.0) + pnl
        wins += item["status"] == "win"
        losses += item["status"] == "loss"
        be += item["status"] == "be"
        timeouts += item["status"] == "timeout"
    worst_day_001 = min(by_day.values()) if by_day else 0.0
    daily_limit = account * daily_pct / 100.0
    total_limit = account * total_pct / 100.0
    lot_by_daily = 999.0 if worst_day_001 >= 0 else 0.01 * daily_limit / abs(worst_day_001)
    lot_by_total = 999.0 if max_dd_001 >= 0 else 0.01 * total_limit / abs(max_dd_001)
    raw_lot = min(lot_by_daily, lot_by_total)
    lot = max(0.01, math.floor(raw_lot * 100.0) / 100.0)
    if lot > 0.01:
        lot = round(lot - 0.01, 2)
    mult = lot / 0.01
    return {
        "events": len(events),
        "wins": wins,
        "losses": losses,
        "be": be,
        "timeouts": timeouts,
        "winrate": round(wins / max(1, wins + losses) * 100.0, 2),
        "p001": round(base_profit, 2),
        "max_dd_001": round(max_dd_001, 2),
        "worst_day_001": round(worst_day_001, 2),
        "recommended_lot": round(lot, 2),
        "profit": round(base_profit * mult, 2),
        "max_dd": round(max_dd_001 * mult, 2),
        "worst_day": round(worst_day_001 * mult, 2),
        "final_balance": round(account + (base_profit * mult), 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--account", type=float, default=25000.0)
    parser.add_argument("--daily-dd", type=float, default=2.5)
    parser.add_argument("--total-dd", type=float, default=5.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    end = datetime.now(UTC)
    start = end - timedelta(days=float(args.days))
    rates = _rates(symbol, start - timedelta(hours=6), end + timedelta(hours=74))

    wanted = [_variants(item) for item in cfg.telegram_watch_channels]
    signals: list[SignalRow] = []
    client = TelegramClient(str((cfg.data_dir / cfg.telegram_session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & item for item in wanted):
                continue
            title = str(getattr(dialog, "title", "") or getattr(dialog.entity, "username", "") or getattr(dialog, "id", ""))
            async for message in client.iter_messages(dialog.entity):
                if message.date and message.date < start:
                    break
                parsed = _parse_signal(
                    str(getattr(message, "raw_text", "") or ""),
                    f"{getattr(dialog, 'id', '')}:{getattr(message, 'id', '')}",
                    int(getattr(dialog, "id", 0) or 0),
                    title,
                    "",
                    int(getattr(message, "id", 0) or 0),
                )
                if parsed is None or parsed.asset != "gold":
                    continue
                idx = int(rates["time"].searchsorted(pd.Timestamp(message.date), side="left"))
                if idx < len(rates):
                    signals.append(SignalRow(message.date, title, int(getattr(message, "id", 0) or 0), parsed, idx))
    finally:
        await client.disconnect()

    entry_modes = ["market_between_entry_tp1", "nearest_pending_15m", "hybrid_market_near_plus_pending"]
    target_plans = [
        (1,), (2,), (3,), (4,),
        (1, 2), (1, 3), (1, 4),
        (1, 2, 3), (1, 2, 4),
        (1, 1, 2), (1, 1, 3), (1, 1, 4), (1, 2, 2),
        (1, 2, 3, 4), (1, 1, 2, 4), (1, 1, 3, 4),
    ]
    protects = ["none", "be"]
    sl_caps = [4.0, 6.0]
    runs: list[dict[str, Any]] = []
    for entry_mode in entry_modes:
        for target_plan in target_plans:
            for protect in protects:
                for sl_cap in sl_caps:
                    events: list[dict[str, Any]] = []
                    skips: dict[str, int] = {}
                    for row in signals:
                        market = float(rates.iloc[row.start_idx]["close"])
                        entries = _entries_for_mode(row.signal, market, entry_mode)
                        if not entries:
                            skips["no_entry"] = skips.get("no_entry", 0) + 1
                            continue
                        leg_defs: list[tuple[float, bool, int]] = []
                        for entry, pending in entries:
                            for target in target_plan:
                                leg_defs.append((entry, pending, int(target)))
                        for entry, pending, target in leg_defs:
                            result = _simulate_leg(row.signal, symbol, rates, row.start_idx, entry, pending, target, protect, sl_cap, 15.0, 72.0)
                            if result["status"] == "skip":
                                reason = str(result.get("reason", "skip"))
                                skips[reason] = skips.get(reason, 0) + 1
                                continue
                            p001 = _profit(symbol, row.signal.side, 0.01, float(result["entry"]), float(result["exit"]))
                            events.append(
                                {
                                    "exit_time": rates.iloc[int(result["exit_idx"])]["time"].isoformat(),
                                    "channel": row.channel,
                                    "message_id": row.message_id,
                                    "status": result["status"],
                                    "p001": p001,
                                }
                            )
                    summary = _score_events(events, float(args.account), float(args.daily_dd), float(args.total_dd))
                    summary.update(
                        {
                            "entry_mode": entry_mode,
                            "target_plan": list(target_plan),
                            "protect": protect,
                            "sl_cap": sl_cap,
                            "skips": skips,
                        }
                    )
                    runs.append(summary)

    feasible = [
        row
        for row in runs
        if abs(float(row["max_dd"])) <= float(args.account) * float(args.total_dd) / 100.0 + 1e-9
        and abs(float(row["worst_day"])) <= float(args.account) * float(args.daily_dd) / 100.0 + 1e-9
        and row["events"] >= 10
    ]
    by_profit = sorted(feasible, key=lambda row: (row["profit"], row["winrate"], -abs(row["max_dd"])), reverse=True)
    by_winrate = sorted(feasible, key=lambda row: (row["winrate"], row["profit"], -abs(row["max_dd"])), reverse=True)
    by_goal = sorted(
        feasible,
        key=lambda row: (
            row["profit"] >= float(args.account) * 0.10,
            row["profit"],
            row["winrate"],
            -abs(row["max_dd"]),
        ),
        reverse=True,
    )
    out = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "days": args.days,
        "account": args.account,
        "limits": {"daily_dd_pct": args.daily_dd, "total_dd_pct": args.total_dd},
        "signals": len(signals),
        "tested": len(runs),
        "feasible": len(feasible),
        "best_by_profit": by_profit[:25],
        "best_by_winrate": by_winrate[:25],
        "best_for_goal": by_goal[:25],
    }
    output = Path(args.output) if args.output else cfg.data_dir / "funded_multi_leg_optimization_30d.json"
    output.write_text(json.dumps(out, indent=2, ensure_ascii=True), encoding="utf-8")
    print(output)
    print(json.dumps({"best_by_profit": by_profit[:5], "best_by_winrate": by_winrate[:5]}, indent=2, ensure_ascii=True))
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
