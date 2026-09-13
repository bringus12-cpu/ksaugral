from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
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


LOTS = (0.01, 0.05, 0.1, 1.0)


@dataclass
class Stats:
    scanned: int = 0
    parsed: int = 0
    eligible: int = 0
    placed: int = 0
    skipped: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    profit_by_lot: dict[str, float] = field(default_factory=lambda: {str(lot): 0.0 for lot in LOTS})
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)


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


def _simulate_one(
    symbol: str,
    signal: ParsedSignal,
    entry: float,
    pending_limit: bool,
    market_price: float,
    start_idx: int,
    rates: pd.DataFrame,
    horizon_hours: int,
) -> dict[str, Any]:
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


def _add(stats: Stats, result: dict[str, Any], symbol: str, side: str, strategy_name: str) -> None:
    if result["status"] == "skipped":
        stats.skipped += 1
        return
    stats.eligible += 1
    stats.placed += 1
    bucket = stats.by_strategy.setdefault(
        strategy_name,
        {"placed": 0, "wins": 0, "losses": 0, "timeouts": 0, "profit_by_lot": {str(lot): 0.0 for lot in LOTS}},
    )
    bucket["placed"] += 1
    if result["status"] == "win":
        stats.wins += 1
        bucket["wins"] += 1
    elif result["status"] == "loss":
        stats.losses += 1
        bucket["losses"] += 1
    else:
        stats.timeouts += 1
        bucket["timeouts"] += 1
    entry = float(result.get("entry", 0.0) or 0.0)
    exit_price = float(result.get("exit", entry) or entry)
    for lot in LOTS:
        value = _profit(symbol, side, lot, entry, exit_price)
        stats.profit_by_lot[str(lot)] += value
        bucket["profit_by_lot"][str(lot)] += value


def _summary(stats: Stats) -> dict[str, Any]:
    by_strategy = {}
    for name, row in stats.by_strategy.items():
        placed = int(row["placed"])
        by_strategy[name] = {
            "placed": placed,
            "wins": int(row["wins"]),
            "losses": int(row["losses"]),
            "timeouts": int(row["timeouts"]),
            "win_rate_pct": round((int(row["wins"]) / placed) * 100.0, 2) if placed else 0.0,
            "profit_by_lot": {lot: round(float(value), 2) for lot, value in row["profit_by_lot"].items()},
        }
    return {
        "scanned": stats.scanned,
        "parsed": stats.parsed,
        "eligible": stats.eligible,
        "placed": stats.placed,
        "skipped": stats.skipped,
        "wins": stats.wins,
        "losses": stats.losses,
        "timeouts": stats.timeouts,
        "win_rate_pct": round((stats.wins / stats.placed) * 100.0, 2) if stats.placed else 0.0,
        "profit_by_lot": {lot: round(float(value), 2) for lot, value in stats.profit_by_lot.items()},
        "by_strategy": by_strategy,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--horizon-hours", type=int, default=72)
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
    total = Stats()
    channels: defaultdict[str, Stats] = defaultdict(Stats)
    titles: dict[str, str] = {}
    client = TelegramClient(str((cfg.data_dir / cfg.telegram_session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & item for item in wanted):
                continue
            token = str(getattr(dialog.entity, "username", "") or getattr(dialog, "id", ""))
            title = str(getattr(dialog, "title", "") or token)
            titles[token] = title
            channel = channels[token]
            async for message in client.iter_messages(dialog.entity):
                if message.date and message.date < start_at:
                    break
                channel.scanned += 1
                total.scanned += 1
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
                channel.parsed += 1
                total.parsed += 1
                timestamp = pd.Timestamp(message.date)
                start_idx = int(rates["time"].searchsorted(timestamp, side="left"))
                if start_idx >= len(rates):
                    continue
                market_price = float(rates.iloc[start_idx]["close"])
                strategy_name = _channel_strategy(parsed).name
                for entry, pending in _entry_plan(parsed, market_price):
                    result = _simulate_one(symbol, parsed, entry, pending, market_price, start_idx, rates, args.horizon_hours)
                    _add(channel, result, symbol, parsed.side, strategy_name)
                    _add(total, result, symbol, parsed.side, strategy_name)
    finally:
        await client.disconnect()
        shutdown()

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "start_utc": start_at.isoformat(),
        "end_utc": end_at.isoformat(),
        "days": args.days,
        "timeframe": args.timeframe,
        "horizon_hours": args.horizon_hours,
        "symbol": symbol,
        "method": "Current bot strategy map. Range signals use three limit entries. Non-range market signals enter at signal candle close. SL/TP conflict in same candle is SL first.",
        "total": _summary(total),
        "channels": [
            {"token": token, "title": titles.get(token, token), **_summary(stats)}
            for token, stats in sorted(channels.items())
        ],
    }
    path = Path(args.output) if args.output else cfg.data_dir / f"current_strategy_backtest_{args.days}d_{args.timeframe.lower()}.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
