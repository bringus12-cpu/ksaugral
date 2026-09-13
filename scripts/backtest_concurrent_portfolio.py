from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from backtest_current_three_leg_projection import _profit, _rates


NEW_CHANNELS = {
    "*XAUUSD SIGNALS*📈🎯",
    "ROYAL GOLD SIGNALS",
    "Gold Signals VIP (XAUUSD FOREX)",
    "NAS100 XAUUSD US30 FOREX SIGNALS",
    "GOLD SIGNALS DAILY XAUUSD",
    "BEN GOLD TRADER",
    "BTCUSD +GOLD FOREX SIGNALS",
    "FX- GOLD ( TRADERS )",
    "GOLD FOREX SIGNALS XAUUSD",
    "XAUUSD SCALPING SIGNALS (GOLD)",
    "XAUUSD Trading Signal FREE 💰",
    "FREE GOLD SIGNALS (XAUUSD)",
    "VIP PREMIUM XAUUSD GOLD",
    "GOLD FOREX TRADING",
    "𝐗𝐚𝐮𝐮𝐬𝐝 (𝐕𝐈𝐏) 𝐒𝐢𝐠𝐧𝐚𝐥𝐬",
    "GOLD BTCUSD XAUUSD VIP SIGNALS",
    "GOLD SCALPING SIGNALS 📈",
    "Gold Scalping Signals 🇬🇧",
    "Gold Pro Trader Forex Signals",
    "GOLD BTCUSD XAUUSD FOREX SIGNALS",
}
MIRROR_CHANNELS = {"GOLD FOREX SIGNALS XAUUSD", "GOLD FOREX TRADING"}


@dataclass
class Position:
    event: dict[str, Any]
    lot: float
    margin: float


def _dt(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC")


def _equity(balance: float, positions: dict[int, Position], price: float, symbol: str) -> float:
    floating = 0.0
    for position in positions.values():
        event = position.event
        floating += _profit(symbol, str(event["side"]), position.lot, float(event["entry"]), price)
    return balance + floating


def _margin(symbol: str, side: str, lot: float, price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_margin(order_type, symbol, lot, price) or 0.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--scope", choices=["new", "combined"], required=True)
    parser.add_argument("--start-balance", type=float, default=300.0)
    parser.add_argument("--equity-step", type=float, default=300.0)
    parser.add_argument("--lot-per-step", type=float, default=0.03)
    parser.add_argument("--spread-price", type=float, default=0.12)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    raw_events = source.get("equity", [])
    events: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_events):
        if not raw.get("signal_time") or not raw.get("entry_time"):
            raise RuntimeError("Input must contain signal_time and entry_time; rerun the projection backtest")
        if raw["channel"] in MIRROR_CHANNELS:
            continue
        if args.scope == "new" and raw["channel"] not in NEW_CHANNELS:
            continue
        event = dict(raw)
        event["id"] = index
        event["signal_ts"] = _dt(event["signal_time"])
        event["entry_ts"] = _dt(event["entry_time"])
        event["exit_ts"] = _dt(event["exit_time"])
        events.append(event)

    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    start = min(event["signal_ts"] for event in events).to_pydatetime()
    end = max(event["exit_ts"] for event in events).to_pydatetime()
    rates = _rates(symbol, "M5", start, end)
    if rates.empty:
        raise RuntimeError("No M5 rates returned from MT5")

    placements: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    activations: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    exits: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for event in events:
        groups.setdefault((event["channel"], int(event["message_id"])), []).append(event)
        activations.setdefault(event["entry_ts"].floor("5min"), []).append(event)
        exits.setdefault(event["exit_ts"].floor("5min"), []).append(event)
    for group in groups.values():
        placements.setdefault(min(event["signal_ts"] for event in group).floor("5min"), []).append(group[0])

    balance = float(args.start_balance)
    peak_equity = balance
    min_equity = balance
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    positions: dict[int, Position] = {}
    group_lot: dict[tuple[str, int], float] = {}
    rejected = 0
    activated = 0
    max_open = 0
    max_used_margin = 0.0
    max_total_signal_lot = 0.0
    spread_paid = 0.0
    stopout = False
    stopout_time = ""
    stopout_level = float(getattr(mt5.account_info(), "margin_so_so", 50.0) or 50.0)

    for _, row in rates.iterrows():
        now = pd.Timestamp(row["time"]).floor("5min")
        price = float(row["close"])

        for representative in placements.get(now, []):
            equity = _equity(balance, positions, price, symbol)
            steps = max(1, math.floor(max(0.0, equity) / float(args.equity_step)))
            total_lot = steps * float(args.lot_per_step)
            leg_lot = normalize_volume(symbol, total_lot / 3.0, cfg.min_lot, max(cfg.max_lot, cfg.signal_dynamic_lot_max))
            key = (representative["channel"], int(representative["message_id"]))
            group_lot[key] = leg_lot
            max_total_signal_lot = max(max_total_signal_lot, leg_lot * 3.0)

        for event in activations.get(now, []):
            key = (event["channel"], int(event["message_id"]))
            lot = group_lot.get(key, float(cfg.min_lot))
            equity = _equity(balance, positions, price, symbol)
            used_margin = sum(position.margin for position in positions.values())
            required = _margin(symbol, str(event["side"]), lot, float(event["entry"]))
            if required > max(0.0, equity - used_margin):
                rejected += 1
                continue
            positions[int(event["id"])] = Position(event=event, lot=lot, margin=required)
            activated += 1

        for event in exits.get(now, []):
            position = positions.pop(int(event["id"]), None)
            if position is None:
                continue
            gross = float(event["profit_001"]) * (position.lot / 0.01)
            spread_cost = abs(_profit(symbol, "buy", position.lot, float(event["entry"]), float(event["entry"]) + args.spread_price))
            balance += gross - spread_cost
            spread_paid += spread_cost

        used_margin = sum(position.margin for position in positions.values())
        equity = _equity(balance, positions, price, symbol)
        max_open = max(max_open, len(positions))
        max_used_margin = max(max_used_margin, used_margin)
        peak_equity = max(peak_equity, equity)
        min_equity = min(min_equity, equity)
        drawdown = equity - peak_equity
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = (drawdown / peak_equity) * 100.0 if peak_equity > 0 else -100.0
        margin_level = (equity / used_margin * 100.0) if used_margin > 0 else 999999.0
        if equity <= 0.0 or margin_level <= stopout_level:
            stopout = True
            stopout_time = now.isoformat()
            break

    final_price = float(rates.iloc[-1]["close"])
    final_equity = _equity(balance, positions, final_price, symbol)
    payload = {
        "scope": args.scope,
        "start_balance": round(float(args.start_balance), 2),
        "final_balance": round(balance, 2),
        "final_equity": round(final_equity, 2),
        "profit_on_equity": round(final_equity - float(args.start_balance), 2),
        "min_equity": round(min_equity, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "signals": len(groups),
        "legs_available": len(events),
        "legs_activated": activated,
        "legs_rejected_margin": rejected,
        "max_simultaneous_positions": max_open,
        "max_used_margin": round(max_used_margin, 2),
        "max_total_signal_lot": round(max_total_signal_lot, 2),
        "spread_paid": round(spread_paid, 2),
        "stopout": stopout,
        "stopout_time": stopout_time,
        "stopout_level_pct": stopout_level,
        "assumption": "0.03 total signal lot per each 300 USD floating equity, split equally across three legs",
    }
    Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    shutdown()


if __name__ == "__main__":
    main()
