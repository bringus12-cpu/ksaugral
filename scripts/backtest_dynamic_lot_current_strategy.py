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
from app.telegram_signal_bot import ParsedSignal, _channel_strategy, _parse_signal


BASE_LOT = 0.01


@dataclass
class Trade:
    time: datetime
    channel: str
    strategy: str
    side: str
    entry: float
    exit: float
    profit_001: float


def _variants(token: str) -> set[str]:
    raw = str(token).strip()
    clean = raw.lower().lstrip("@")
    out = {clean}
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if clean.startswith(prefix):
            out.add(clean[len(prefix) :].strip("/"))
    if raw.startswith("-100"):
        out.add(raw[4:])
    return {item for item in out if item}


def _dialog_variants(dialog: Any) -> set[str]:
    did = str(getattr(dialog, "id", "") or "")
    username = str(getattr(dialog.entity, "username", "") or "").lower()
    out = {did, did.lstrip("-"), username}
    if did.startswith("-100"):
        out.add(did[4:])
    return {item for item in out if item}


def _timeframe(value: str) -> int:
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
    }[value.upper()]


def _rates(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, _timeframe(timeframe), start, end)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
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


def _valid(side: str, entry: float, sl: float, tps: list[float], target_index: int) -> bool:
    if entry <= 0 or sl <= 0 or len(tps) < target_index:
        return False
    direction = 1 if side == "buy" else -1
    return (entry - sl) * direction > 0 and (tps[target_index - 1] - entry) * direction > 0


def _entry_touched(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _better_stop(side: str, current_sl: float, candidate: float) -> float:
    if current_sl <= 0:
        return candidate
    return max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)


def _limit_is_valid(side: str, entry: float, market_price: float) -> bool:
    return entry < market_price if side == "buy" else entry > market_price


def _entry_plan(signal: ParsedSignal, market_price: float) -> list[tuple[float, bool]]:
    strategy = _channel_strategy(signal)
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    if strategy.force_all_entries and len(entries) > 1:
        return [(entry, True) for entry in entries]
    if signal.order_kind in {"limit", "stop"} and float(signal.entry or 0.0) > 0:
        return [(float(signal.entry), True)]
    return [(float(market_price), False)]


def _simulate_one(signal: ParsedSignal, entry: float, pending_limit: bool, market_price: float, start_idx: int, rates: pd.DataFrame, horizon_hours: int) -> dict[str, Any]:
    strategy = _channel_strategy(signal)
    if pending_limit and strategy.limit_only_ranges and not _limit_is_valid(signal.side, entry, market_price):
        return {"status": "skipped"}
    sl = float(signal.sl or 0.0)
    tps = [float(tp) for tp in signal.tps[:4]]
    if not _valid(signal.side, entry, sl, tps, strategy.target_index):
        return {"status": "skipped"}

    times = rates["time"]
    end_time = times.iloc[start_idx] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(times.searchsorted(end_time, side="right")), len(rates))
    triggered = start_idx
    if pending_limit:
        triggered = -1
        for idx in range(start_idx, end_idx):
            row = rates.iloc[idx]
            if _entry_touched(entry, float(row["high"]), float(row["low"])):
                triggered = idx
                break
        if triggered < 0:
            return {"status": "skipped"}

    target = tps[strategy.target_index - 1]
    tp1 = tps[0]
    tp1_seen = False
    current_sl = sl
    for idx in range(triggered, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        atr = float(row.get("atr14", 0.0) or 0.0)
        if _hit_sl(signal.side, current_sl, high, low):
            return {"status": "loss", "entry": entry, "exit": current_sl}
        if _hit_tp(signal.side, target, high, low):
            return {"status": "win", "entry": entry, "exit": target}
        if _hit_tp(signal.side, tp1, high, low):
            tp1_seen = True
        if tp1_seen and strategy.protect_mode == "tp1":
            current_sl = _better_stop(signal.side, current_sl, tp1)
        elif tp1_seen and strategy.protect_mode == "be":
            current_sl = _better_stop(signal.side, current_sl, entry)
        elif tp1_seen and strategy.protect_mode == "atr" and atr > 0:
            candidate = close - (atr * strategy.atr_mult) if signal.side == "buy" else close + (atr * strategy.atr_mult)
            current_sl = _better_stop(signal.side, current_sl, candidate)

    close = float(rates.iloc[max(triggered, end_idx - 1)]["close"])
    return {"status": "timeout", "entry": entry, "exit": close}


def _lot_for_balance(net_profit: float, step_usd: float, base_lot: float, add_lot: float, max_lot: float) -> float:
    increments = max(0, math.floor(float(net_profit) / float(step_usd)))
    return round(min(float(max_lot), float(base_lot) + increments * float(add_lot)), 2)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--step-usd", type=float, default=200.0)
    parser.add_argument("--base-lot", type=float, default=0.01)
    parser.add_argument("--add-lot", type=float, default=0.01)
    parser.add_argument("--max-lot", type=float, default=100.0)
    parser.add_argument("--profitable-channels-only", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=args.days)
    rates = _rates(symbol, args.timeframe, start_at - timedelta(days=2), end_at + timedelta(hours=args.horizon_hours + 2))
    if rates.empty:
        raise RuntimeError("No rates returned from MT5")

    wanted = [_variants(item) for item in cfg.telegram_watch_channels]
    trades: list[Trade] = []
    client = TelegramClient(str((cfg.data_dir / cfg.telegram_session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & item for item in wanted):
                continue
            token = str(getattr(dialog.entity, "username", "") or getattr(dialog, "id", ""))
            title = str(getattr(dialog, "title", "") or token)
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
                start_idx = int(rates["time"].searchsorted(pd.Timestamp(message.date), side="left"))
                if start_idx >= len(rates):
                    continue
                market_price = float(rates.iloc[start_idx]["close"])
                strategy_name = _channel_strategy(parsed).name
                for entry, pending in _entry_plan(parsed, market_price):
                    result = _simulate_one(parsed, entry, pending, market_price, start_idx, rates, args.horizon_hours)
                    if result.get("status") == "skipped":
                        continue
                    entry_price = float(result.get("entry", 0.0) or 0.0)
                    exit_price = float(result.get("exit", entry_price) or entry_price)
                    trades.append(
                        Trade(
                            time=message.date,
                            channel=token,
                            strategy=strategy_name,
                            side=parsed.side,
                            entry=entry_price,
                            exit=exit_price,
                            profit_001=_profit(symbol, parsed.side, BASE_LOT, entry_price, exit_price),
                        )
                    )
    finally:
        await client.disconnect()
        shutdown()

    fixed_by_channel: dict[str, float] = {}
    for trade in trades:
        fixed_by_channel[trade.channel] = fixed_by_channel.get(trade.channel, 0.0) + trade.profit_001
    selected_channels = sorted(channel for channel, profit in fixed_by_channel.items() if profit > 0)
    if args.profitable_channels_only:
        selected = set(selected_channels)
        trades = [trade for trade in trades if trade.channel in selected]

    trades.sort(key=lambda trade: trade.time)
    net = 0.0
    peak = 0.0
    max_dd = 0.0
    max_lot_used = args.base_lot
    by_channel: dict[str, dict[str, Any]] = {}
    equity_points = []
    for trade in trades:
        lot = _lot_for_balance(net, args.step_usd, args.base_lot, args.add_lot, args.max_lot)
        max_lot_used = max(max_lot_used, lot)
        profit = trade.profit_001 * (lot / BASE_LOT)
        net += profit
        peak = max(peak, net)
        max_dd = min(max_dd, net - peak)
        row = by_channel.setdefault(trade.channel, {"trades": 0, "profit": 0.0})
        row["trades"] += 1
        row["profit"] += profit
        equity_points.append({"time": trade.time.isoformat(), "lot": lot, "profit": round(profit, 2), "balance_net": round(net, 2)})

    fixed_profit_001 = sum(trade.profit_001 for trade in trades)
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "days": args.days,
        "timeframe": args.timeframe,
        "horizon_hours": args.horizon_hours,
        "symbol": symbol,
        "rule": f"Start {args.base_lot} lot, add {args.add_lot} lot for each +{args.step_usd} USD net balance profit.",
        "profitable_channels_only": bool(args.profitable_channels_only),
        "selected_channels": selected_channels if args.profitable_channels_only else [],
        "fixed_profit_by_channel_0_01": {key: round(value, 2) for key, value in sorted(fixed_by_channel.items(), key=lambda item: item[1], reverse=True)},
        "trades": len(trades),
        "fixed_0_01_profit": round(fixed_profit_001, 2),
        "dynamic_profit": round(net, 2),
        "max_drawdown_from_peak": round(max_dd, 2),
        "max_lot_used": round(max_lot_used, 2),
        "final_lot_next_trade": _lot_for_balance(net, args.step_usd, args.base_lot, args.add_lot, args.max_lot),
        "by_channel": {key: {"trades": value["trades"], "profit": round(value["profit"], 2)} for key, value in sorted(by_channel.items(), key=lambda item: item[1]["profit"], reverse=True)},
        "equity_points": equity_points,
    }
    path = Path(args.output) if args.output else cfg.data_dir / f"dynamic_lot_current_strategy_{args.days}d_{args.timeframe.lower()}.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
