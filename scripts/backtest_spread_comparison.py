from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from app.telegram_signal_bot import THREE_LEG_TARGET_PLAN, _channel_strategy, _parse_signal, _select_live_tps
from backtest_current_three_leg_projection import (
    _dialog_variants,
    _profit,
    _rates,
    _simulate_leg,
    _variants,
)


def _placement_plan(signal: Any, market_price: float, entry_mode: str) -> list[tuple[float, bool, str, int, str]]:
    strategy = _channel_strategy(signal)
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    default_entry = float(signal.entry or 0.0) or market_price
    target_plan = [(target, protect) for target, protect in THREE_LEG_TARGET_PLAN]
    if strategy.split_target_indices:
        target_plan = [
            (int(target), "atr" if index == 4 and "phoenix" in signal.chat_title.lower() else ("be" if index > 1 else "none"))
            for index, target in enumerate(strategy.split_target_indices, start=1)
        ]

    use_all_entries = (entry_mode == "all3" or strategy.force_all_entries) and len(entries) > 1
    if use_all_entries:
        planned_entries = entries[: len(target_plan)]
        if len(planned_entries) < len(target_plan):
            planned_entries.extend([planned_entries[-1]] * (len(target_plan) - len(planned_entries)))
        order_kind = signal.order_kind if signal.order_kind in {"limit", "stop"} else "limit"
        return [(entry, True, order_kind, target, protect) for entry, (target, protect) in zip(planned_entries, target_plan)]

    pending = signal.order_kind in {"limit", "stop"}
    order_kind = signal.order_kind if pending else "market"
    entry = default_entry if pending else market_price
    return [(entry, pending, order_kind, target, protect) for target, protect in target_plan]


def _dynamic_position_lot(cfg: Any, symbol: str, balance: float) -> float:
    if not bool(getattr(cfg, "signal_dynamic_lot_enabled", False)):
        return normalize_volume(symbol, float(cfg.signal_fixed_lot), float(cfg.min_lot), float(cfg.max_lot))
    steps = max(0, math.floor(max(0.0, balance) / float(cfg.signal_dynamic_lot_step_usd)))
    raw = max(float(cfg.signal_fixed_lot), steps * float(cfg.signal_dynamic_lot_add))
    raw = min(float(cfg.signal_dynamic_lot_max), raw)
    return normalize_volume(symbol, raw, float(cfg.min_lot), max(float(cfg.max_lot), float(cfg.signal_dynamic_lot_max)))


def _run_equity(
    events: list[dict[str, Any]],
    cfg: Any,
    symbol: str,
    start_balance: float,
    spread_price: float,
    fixed_lot: float = 0.0,
    max_daily_dd_pct: float = 0.0,
    max_total_dd_pct: float = 0.0,
) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_drawdown = 0.0
    spread_paid = 0.0
    max_lot = 0.0
    bankrupt = False
    halted = False
    halt_reason = ""
    halt_time = ""
    processed = 0
    day_key = ""
    day_start_balance = balance
    max_daily_drawdown_pct = 0.0

    for event in events:
        if halted:
            break
        event_time = event.get("exit_time")
        event_day = event_time.date().isoformat() if event_time else ""
        if event_day != day_key:
            day_key = event_day
            day_start_balance = balance
        lot = (
            normalize_volume(symbol, fixed_lot, float(cfg.min_lot), float(cfg.max_lot))
            if fixed_lot > 0
            else _dynamic_position_lot(cfg, symbol, balance)
        )
        gross = float(event["profit_001"]) * (lot / 0.01)
        spread_cost = 0.0
        if spread_price > 0:
            spread_cost = abs(_profit(symbol, "buy", lot, float(event["entry"]), float(event["entry"]) + spread_price))
        net = gross - spread_cost
        balance += net
        spread_paid += spread_cost
        max_lot = max(max_lot, lot)
        peak = max(peak, balance)
        max_drawdown = min(max_drawdown, balance - peak)
        processed += 1
        daily_dd_pct = max(0.0, (day_start_balance - balance) / max(1.0, day_start_balance) * 100.0)
        total_dd_pct = max(0.0, (start_balance - balance) / max(1.0, start_balance) * 100.0)
        max_daily_drawdown_pct = max(max_daily_drawdown_pct, daily_dd_pct)
        if balance <= 0:
            bankrupt = True
            break
        if max_daily_dd_pct > 0 and daily_dd_pct >= max_daily_dd_pct:
            halted = True
            halt_reason = "max_daily_drawdown"
        elif max_total_dd_pct > 0 and total_dd_pct >= max_total_dd_pct:
            halted = True
            halt_reason = "max_total_drawdown"
        if halted:
            halt_time = event_time.isoformat() if event_time else ""

    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "profit_percent": round(((balance / start_balance) - 1.0) * 100.0, 2),
        "spread_paid": round(spread_paid, 2),
        "max_drawdown_from_peak": round(max_drawdown, 2),
        "max_daily_drawdown_pct": round(max_daily_drawdown_pct, 2),
        "max_lot_per_position": round(max_lot, 2),
        "processed_legs": processed,
        "bankrupt": bankrupt,
        "halted": halted,
        "halt_reason": halt_reason,
        "halt_time_utc": halt_time,
        "unprocessed_legs_after_halt": max(0, len(events) - processed),
    }


def _spread_aware_market_ok(signal: Any, entry: float, target: int, spread_price: float) -> tuple[bool, str]:
    if spread_price <= 0:
        return True, ""
    try:
        tp1, _target, _live_index, _live_tps = _select_live_tps(signal.side, float(entry), signal.tps, int(target))
    except Exception as exc:
        return False, f"no_live_tp:{exc}"
    reward = abs(float(tp1) - float(entry))
    min_net = float(__import__("os").getenv("SIGNAL_MIN_NET_TP1_AFTER_SPREAD_USD", "1.0") or 1.0)
    min_mult = float(__import__("os").getenv("SIGNAL_MIN_TP1_SPREAD_MULT", "2.2") or 2.2)
    required_reward = max(min_net + float(spread_price), float(spread_price) * min_mult)
    if reward + 1e-9 < required_reward:
        return False, f"weak_market_tp1_after_spread:{reward:.2f}<{required_reward:.2f}"
    return True, ""


def _channel_summary(events: list[dict[str, Any]], symbol: str, spread_price: float, lot: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for event in events:
        channel = str(event.get("channel") or "")
        row = out.setdefault(channel, {"legs": 0, "wins": 0, "losses": 0, "timeouts": 0, "profit": 0.0, "spread_paid": 0.0})
        row["legs"] += 1
        row["wins"] += 1 if event.get("status") == "win" else 0
        row["losses"] += 1 if event.get("status") == "loss" else 0
        row["timeouts"] += 1 if event.get("status") == "timeout" else 0
        gross = float(event["profit_001"]) * (float(lot) / 0.01)
        spread_cost = abs(_profit(symbol, "buy", lot, float(event["entry"]), float(event["entry"]) + spread_price)) if spread_price > 0 else 0.0
        row["profit"] += gross - spread_cost
        row["spread_paid"] += spread_cost
    return {
        key: {
            "legs": value["legs"],
            "wins": value["wins"],
            "losses": value["losses"],
            "timeouts": value["timeouts"],
            "win_rate": round(value["wins"] / max(1, value["wins"] + value["losses"]) * 100.0, 2),
            "profit": round(value["profit"], 2),
            "spread_paid": round(value["spread_paid"], 2),
        }
        for key, value in sorted(out.items(), key=lambda item: item[1]["profit"], reverse=True)
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--timeframe", default="M1", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--start-balance", type=float, default=300.0)
    parser.add_argument("--spread-aware", action="store_true")
    parser.add_argument("--fixed-lot", type=float, default=0.0)
    parser.add_argument("--max-daily-dd-pct", type=float, default=0.0)
    parser.add_argument("--max-total-dd-pct", type=float, default=0.0)
    parser.add_argument("--watch-channels", default="", help="Comma-separated channel ids or usernames for this run only.")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    if str(args.watch_channels or "").strip():
        os.environ["WATCH_CHANNELS"] = str(args.watch_channels)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    spread_price = max(0.0, float(tick.ask) - float(tick.bid))
    point = float(getattr(info, "point", 0.01) or 0.01)
    spread_points = spread_price / point

    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=float(args.days))
    rates = _rates(symbol, args.timeframe, start_at - timedelta(hours=6), end_at + timedelta(hours=args.horizon_hours + 2))
    if rates.empty:
        raise RuntimeError("No XAUUSD rates returned from MT5")

    wanted = [_variants(item) for item in cfg.telegram_watch_channels]
    events: list[dict[str, Any]] = []
    parsed_signals = 0
    gold_signals = 0
    skipped: dict[str, int] = {}

    source = cfg.data_dir / f"{cfg.telegram_session_name}.session"
    copy = cfg.data_dir / f"{cfg.telegram_session_name}_spread_compare.session"
    if source.exists():
        shutil.copy2(source, copy)
    client = TelegramClient(str(copy.with_suffix("").resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & variants for variants in wanted):
                continue
            title = str(getattr(dialog, "title", "") or getattr(dialog, "id", ""))
            async for message in client.iter_messages(dialog.entity):
                message_date = getattr(message, "date", None)
                if message_date and message_date < start_at:
                    break
                signal = _parse_signal(
                    str(getattr(message, "raw_text", "") or ""),
                    f"{getattr(dialog, 'id', '')}:{getattr(message, 'id', '')}",
                    int(getattr(dialog, "id", 0) or 0),
                    title,
                    "",
                    int(getattr(message, "id", 0) or 0),
                )
                if signal is None:
                    continue
                parsed_signals += 1
                if signal.asset != "gold":
                    continue
                gold_signals += 1
                start_idx = int(rates["time"].searchsorted(pd.Timestamp(message_date), side="left"))
                if start_idx >= len(rates):
                    continue
                market_price = float(rates.iloc[start_idx]["close"])
                for entry, pending, order_kind, target, protect in _placement_plan(signal, market_price, cfg.signal_entry_mode):
                    if args.spread_aware and not pending:
                        ok, reason = _spread_aware_market_ok(signal, float(entry), int(target), spread_price)
                        if not ok:
                            skipped[reason] = skipped.get(reason, 0) + 1
                            continue
                    result = _simulate_leg(
                        signal,
                        symbol,
                        float(entry),
                        pending,
                        order_kind,
                        int(target),
                        protect,
                        start_idx,
                        rates,
                        cfg,
                        int(args.horizon_hours),
                    )
                    if result["status"] in {"skipped", "expired"}:
                        reason = str(result.get("reason") or result["status"])
                        skipped[reason] = skipped.get(reason, 0) + 1
                        continue
                    exit_idx = int(result["exit_idx"])
                    entry_price = float(result["entry"])
                    exit_price = float(result["exit"])
                    events.append({
                        "exit_time": rates.iloc[exit_idx]["time"].to_pydatetime(),
                        "channel": title,
                        "message_id": int(getattr(message, "id", 0) or 0),
                        "status": str(result["status"]),
                        "pending": bool(pending),
                        "target": int(target),
                        "entry": entry_price,
                        "exit": exit_price,
                        "profit_001": _profit(symbol, signal.side, 0.01, entry_price, exit_price),
                    })
    finally:
        await client.disconnect()

    events.sort(key=lambda item: item["exit_time"])
    no_spread = _run_equity(
        events, cfg, symbol, float(args.start_balance), 0.0, float(args.fixed_lot), float(args.max_daily_dd_pct), float(args.max_total_dd_pct)
    )
    current_spread = _run_equity(
        events, cfg, symbol, float(args.start_balance), spread_price, float(args.fixed_lot), float(args.max_daily_dd_pct), float(args.max_total_dd_pct)
    )
    summary_lot = float(args.fixed_lot or cfg.signal_fixed_lot or 0.01)
    wins = sum(1 for event in events if event["status"] == "win")
    losses = sum(1 for event in events if event["status"] == "loss")
    timeouts = sum(1 for event in events if event["status"] == "timeout")

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "symbol": symbol,
        "timeframe": args.timeframe,
        "horizon_hours": args.horizon_hours,
        "watched_channels": list(cfg.telegram_watch_channels),
        "lot_rule": (
            f"fixed {args.fixed_lot:.2f} per position"
            if float(args.fixed_lot) > 0
            else f"fixed {cfg.signal_fixed_lot:.2f} per position"
            if not bool(getattr(cfg, "signal_dynamic_lot_enabled", False))
            else f"per position: max({cfg.signal_fixed_lot}, floor(balance/{cfg.signal_dynamic_lot_step_usd})*{cfg.signal_dynamic_lot_add})"
        ),
        "spread_snapshot": {"points": round(spread_points, 2), "price": round(spread_price, 5)},
        "spread_aware": bool(args.spread_aware),
        "drawdown_limits": {"daily_pct": float(args.max_daily_dd_pct), "total_pct": float(args.max_total_dd_pct)},
        "summary_lot": summary_lot,
        "parsed_signals": parsed_signals,
        "gold_signals": gold_signals,
        "executed_legs": len(events),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "win_rate": round(wins / max(1, wins + losses) * 100.0, 2),
        "skipped": skipped,
        "without_spread": no_spread,
        "with_current_spread": current_spread,
        "by_channel_fixed_lot_with_spread": _channel_summary(events, symbol, spread_price, summary_lot),
    }
    path = Path(args.output) if args.output else cfg.data_dir / "spread_comparison_30d_start300.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(output, indent=2, ensure_ascii=True))
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
