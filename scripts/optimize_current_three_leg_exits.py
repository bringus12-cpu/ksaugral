from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from app.telegram_signal_bot import ParsedSignal, _channel_strategy, _parse_signal
from backtest_current_three_leg_projection import (
    _auto_sl_from_history,
    _dialog_variants,
    _pending_valid,
    _profit,
    _rates,
    _select_live_tps,
    _simulate_leg,
    _variants,
)


TARGET_PLANS = {
    "tp1_tp2_tp4": (1, 2, 4),
    "tp1_tp2_tp3": (1, 2, 3),
    "tp1_tp3_tp4": (1, 3, 4),
    "tp2_tp3_tp4": (2, 3, 4),
    "tp1_all": (1, 1, 1),
    "tp2_all": (2, 2, 2),
    "tp3_all": (3, 3, 3),
    "tp4_all": (4, 4, 4),
    "tp1_tp1_tp2": (1, 1, 2),
    "tp1_tp1_tp3": (1, 1, 3),
    "tp1_tp1_tp4": (1, 1, 4),
    "tp1_tp2_tp2": (1, 2, 2),
}

PROTECT_PLANS = {
    "none": ("none", "none", "none"),
    "be_after_tp1": ("none", "be", "be"),
    "tp1_after_tp1": ("none", "tp1", "tp1"),
    "leg2_tp1_only": ("none", "tp1", "none"),
}

SL_MODES = {
    "signal_sl": None,
    "tight_050": 0.50,
    "tight_075": 0.75,
    "wide_125": 1.25,
    "wide_150": 1.50,
    "auto_atr": 0.0,
}


def _adjusted_signal(signal: ParsedSignal, sl_mode: str, entry: float, symbol: str, row: pd.Series, cfg: Any) -> ParsedSignal:
    if sl_mode == "signal_sl":
        return signal
    if sl_mode == "auto_atr":
        return replace(signal, sl=0.0)
    factor = SL_MODES[sl_mode]
    original = float(signal.sl or 0.0)
    if original <= 0:
        return signal
    distance = abs(float(entry) - original) * float(factor)
    if distance <= 0:
        return signal
    sl = float(entry) - distance if signal.side == "buy" else float(entry) + distance
    info = mt5.symbol_info(symbol)
    digits = int(getattr(info, "digits", 2) or 2)
    return replace(signal, sl=round(sl, digits))


def _plan_for_config(signal: ParsedSignal, market_price: float, target_plan: tuple[int, int, int], protect_plan: tuple[str, str, str]) -> list[tuple[float, bool, str, int, str]]:
    strategy = _channel_strategy(signal)
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    default_entry = float(signal.entry or 0.0) or market_price
    use_all_entries = strategy.force_all_entries and len(entries) > 1 and not strategy.split_target_indices
    if use_all_entries:
        planned = entries[: len(target_plan)]
        if len(planned) < len(target_plan):
            planned.extend([planned[-1]] * (len(target_plan) - len(planned)))
        return [(entry, True, "limit", target, protect) for entry, target, protect in zip(planned, target_plan, protect_plan)]
    if signal.order_kind in {"limit", "stop"} and default_entry > 0:
        entry = default_entry
        is_pending = True
        order_kind = signal.order_kind
    else:
        entry = market_price
        is_pending = False
        order_kind = "market"
    return [(entry, is_pending, order_kind, target, protect) for target, protect in zip(target_plan, protect_plan)]


def _lot_for_net(net_profit: float, base_lot: float, step_usd: float, add_lot: float, max_lot: float) -> float:
    steps = max(0, math.floor(max(0.0, net_profit) / step_usd))
    return min(max_lot, base_lot + (steps * add_lot))


def _score(results: list[dict[str, Any]], start_balance: float, cfg: Any, symbol: str) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_dd = 0.0
    wins = losses = timeouts = 0
    lot_max = max(float(cfg.max_lot), float(cfg.signal_dynamic_lot_max))
    by_channel: dict[str, dict[str, Any]] = {}
    for row in sorted(results, key=lambda item: item["exit_time"]):
        net_profit = balance - float(start_balance)
        total_lot = _lot_for_net(net_profit, float(cfg.signal_fixed_lot), float(cfg.signal_dynamic_lot_step_usd), float(cfg.signal_dynamic_lot_add), lot_max)
        total_lot = normalize_volume(symbol, total_lot, float(cfg.min_lot), lot_max)
        leg_lot = normalize_volume(symbol, total_lot / 3.0, float(cfg.min_lot), lot_max)
        profit = float(row["profit_001"]) * (leg_lot / 0.01)
        balance += profit
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
        wins += 1 if row["status"] == "win" else 0
        losses += 1 if row["status"] == "loss" else 0
        timeouts += 1 if row["status"] == "timeout" else 0
        bucket = by_channel.setdefault(row["channel"], {"legs": 0, "profit": 0.0})
        bucket["legs"] += 1
        bucket["profit"] += profit
    return {
        "legs": len(results),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "final_balance": round(balance, 2),
        "profit": round(balance - float(start_balance), 2),
        "profit_percent": round(((balance / float(start_balance)) - 1.0) * 100.0, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_to_dd": round((balance - float(start_balance)) / abs(max_dd), 3) if max_dd < 0 else 999.0,
        "by_channel": {k: {"legs": v["legs"], "profit": round(v["profit"], 2)} for k, v in sorted(by_channel.items(), key=lambda item: item[1]["profit"], reverse=True)},
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=6.0)
    parser.add_argument("--timeframe", default="M5", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--start-balance", type=float, default=300.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=float(args.days))
    rates = _rates(symbol, args.timeframe, start_at - timedelta(hours=6), end_at + timedelta(hours=args.horizon_hours + 2))
    if rates.empty:
        raise RuntimeError("No XAUUSD rates returned from MT5")

    wanted = [_variants(item) for item in cfg.telegram_watch_channels]
    signals: list[dict[str, Any]] = []
    parsed_all = 0
    client = TelegramClient(str((cfg.data_dir / cfg.telegram_session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & item for item in wanted):
                continue
            title = str(getattr(dialog, "title", "") or getattr(dialog.entity, "username", "") or getattr(dialog, "id", ""))
            async for message in client.iter_messages(dialog.entity):
                if message.date and message.date < start_at:
                    break
                parsed = _parse_signal(
                    str(getattr(message, "raw_text", "") or ""),
                    f"{getattr(dialog, 'id', '')}:{getattr(message, 'id', '')}",
                    int(getattr(dialog, "id", 0) or 0),
                    title,
                    "",
                    int(getattr(message, "id", 0) or 0),
                )
                if parsed is None:
                    continue
                parsed_all += 1
                if parsed.asset != "gold":
                    continue
                start_idx = int(rates["time"].searchsorted(pd.Timestamp(message.date), side="left"))
                if start_idx >= len(rates):
                    continue
                signals.append({"signal": parsed, "time": message.date, "start_idx": start_idx, "channel": title, "message_id": int(getattr(message, "id", 0) or 0)})
    finally:
        await client.disconnect()

    runs: list[dict[str, Any]] = []
    for target_name, target_plan in TARGET_PLANS.items():
        for protect_name, protect_plan in PROTECT_PLANS.items():
            for sl_mode in SL_MODES:
                leg_rows: list[dict[str, Any]] = []
                skip_reasons: dict[str, int] = {}
                for item in signals:
                    signal = item["signal"]
                    start_idx = item["start_idx"]
                    market_price = float(rates.iloc[start_idx]["close"])
                    for entry, is_pending, order_kind, target_index, protect_mode in _plan_for_config(signal, market_price, target_plan, protect_plan):
                        adjusted = _adjusted_signal(signal, sl_mode, float(entry), symbol, rates.iloc[start_idx], cfg)
                        result = _simulate_leg(adjusted, symbol, float(entry), is_pending, order_kind, int(target_index), protect_mode, start_idx, rates, cfg, int(args.horizon_hours))
                        if result["status"] in {"skipped", "expired"}:
                            key = str(result.get("reason") or result["status"])
                            skip_reasons[key] = skip_reasons.get(key, 0) + 1
                            continue
                        exit_idx = int(result["exit_idx"])
                        leg_rows.append(
                            {
                                "exit_time": rates.iloc[exit_idx]["time"].to_pydatetime(),
                                "channel": item["channel"],
                                "message_id": item["message_id"],
                                "status": str(result["status"]),
                                "profit_001": _profit(symbol, signal.side, 0.01, float(result["entry"]), float(result["exit"])),
                            }
                        )
                summary = _score(leg_rows, float(args.start_balance), cfg, symbol)
                summary.update({"target_plan": target_name, "protect_plan": protect_name, "sl_mode": sl_mode, "skip_reasons": skip_reasons})
                runs.append(summary)

    ranked_profit = sorted(runs, key=lambda row: (row["profit"], row["profit_to_dd"]), reverse=True)
    ranked_balanced = sorted(runs, key=lambda row: (row["profit_to_dd"], row["profit"]), reverse=True)
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "symbol": symbol,
        "timeframe": args.timeframe,
        "start_balance": round(float(args.start_balance), 2),
        "parsed_signals_all_assets": parsed_all,
        "gold_signals": len(signals),
        "tested_combinations": len(runs),
        "best_by_profit": ranked_profit[:20],
        "best_balanced": ranked_balanced[:20],
        "all_runs": ranked_profit,
    }
    path = Path(args.output) if args.output else cfg.data_dir / f"optimized_three_leg_exits_{int(args.days)}d_{args.timeframe.lower()}.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
