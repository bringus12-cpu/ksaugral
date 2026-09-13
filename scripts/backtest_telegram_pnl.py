from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from telethon import TelegramClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings
from app.indicators import atr
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _parse_signal


XAU_RE = re.compile(r"\b(?:xau\s*/?\s*usd|xauusd|xau|gold)\b", re.I)
LOTS = (0.01, 0.05, 0.1, 1.0)
TARGETS = (1, 2, 3, 4)


@dataclass
class TargetStats:
    eligible: int = 0
    wins: int = 0
    losses: int = 0
    end_closes: int = 0
    no_entry: int = 0
    profit_by_lot: dict[str, float] = field(default_factory=lambda: {str(lot): 0.0 for lot in LOTS})


@dataclass
class ChannelStats:
    token: str
    title: str
    username: str
    dialog_id: int
    scanned: int = 0
    xau_mentions: int = 0
    parsed: int = 0
    targets: dict[str, TargetStats] = field(default_factory=lambda: {str(target): TargetStats() for target in TARGETS})


def _safe_console(text: str, limit: int = 90) -> str:
    return (text or "").encode("ascii", "ignore").decode("ascii")[:limit]


def _token_variants(token: str) -> set[str]:
    raw = token.strip()
    clean = raw.lower().lstrip("@")
    variants = {clean}
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if clean.startswith(prefix):
            variants.add(clean[len(prefix):])
    if raw.startswith("-100"):
        variants.add(raw[4:])
    return {item for item in variants if item}


def _dialog_variants(dialog: Any) -> set[str]:
    username = str(getattr(dialog.entity, "username", "") or "").lower()
    dialog_id = str(getattr(dialog, "id", "") or "")
    variants = {username, dialog_id, dialog_id.lstrip("-")}
    if dialog_id.startswith("-100"):
        variants.add(dialog_id[4:])
    return {item for item in variants if item}


def _dialog_token(dialog: Any) -> str:
    username = str(getattr(dialog.entity, "username", "") or "")
    return username or str(getattr(dialog, "id", "") or "")


async def _resolve_dialogs(client: TelegramClient, tokens: tuple[str, ...]) -> list[Any]:
    wanted = [_token_variants(token) for token in tokens]
    selected: list[Any] = []
    selected_ids: set[int] = set()
    async for dialog in client.iter_dialogs():
        variants = _dialog_variants(dialog)
        if any(variants & item for item in wanted):
            dialog_id = int(getattr(dialog, "id", 0) or 0)
            if dialog_id not in selected_ids:
                selected.append(dialog)
                selected_ids.add(dialog_id)
    return selected


def _timeframe(name: str) -> int:
    mapping = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
    }
    return mapping[name.upper()]


def _rates(symbol: str, timeframe: str, cutoff: datetime, end_at: datetime, horizon_hours: int) -> pd.DataFrame:
    tf = _timeframe(timeframe)
    raw = mt5.copy_rates_range(symbol, tf, cutoff - timedelta(days=2), end_at + timedelta(hours=horizon_hours + 2))
    if raw is None or len(raw) == 0:
        raw = mt5.copy_rates_from_pos(symbol, tf, 0, 200000)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.sort_values("time").reset_index(drop=True)


def _atr_rates(rates: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if rates.empty:
        return pd.DataFrame()
    if timeframe.upper() != "M1":
        return rates[["time", "open", "high", "low", "close"]].copy()
    frame = rates.set_index("time")
    resampled = frame.resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    return resampled.dropna().reset_index()


def _auto_sl(atr_frame: pd.DataFrame, msg_time: datetime, side: str, entry: float, point: float, digits: int) -> float:
    cutoff = pd.Timestamp(msg_time)
    history = atr_frame[atr_frame["time"] <= cutoff].tail(180).copy()
    atr_value = 0.0
    if len(history) >= 20:
        atr_series = atr(history, 14)
        atr_value = float(atr_series.iloc[-1] or 0.0)
    min_buffer = point * 50.0
    offset = max(min_buffer, atr_value * 1.5 if atr_value > 0 else 0.0)
    if side == "buy":
        return round(entry - offset, digits)
    return round(entry + offset, digits)


def _level_hit(side: str, target: float, high: float, low: float) -> bool:
    return high >= target if side == "buy" else low <= target


def _sl_hit(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price)
    return float(value or 0.0)


def _simulate_target(
    symbol: str,
    signal: Any,
    msg_time: datetime,
    rates: pd.DataFrame,
    atr_frame: pd.DataFrame,
    point: float,
    digits: int,
    target_index: int,
    horizon_hours: int,
) -> dict[str, Any]:
    if len(signal.tps) < target_index:
        return {"status": "missing_tp"}
    if rates.empty:
        return {"status": "no_rates"}
    times = rates["time"]
    timestamp = pd.Timestamp(msg_time)
    if timestamp < times.iloc[0]:
        return {"status": "no_rates"}
    start_idx = int(times.searchsorted(timestamp, side="left"))
    if start_idx >= len(rates):
        return {"status": "no_rates"}

    entry = float(signal.entry or 0.0)
    if entry <= 0:
        entry = float(rates.iloc[start_idx]["close"])
    tp = float(signal.tps[target_index - 1])
    sl = float(signal.sl or 0.0)
    if sl <= 0:
        sl = _auto_sl(atr_frame, msg_time, signal.side, entry, point, digits)

    direction = 1 if signal.side == "buy" else -1
    if (tp - entry) * direction <= 0 or (entry - sl) * direction <= 0:
        return {"status": "no_entry"}

    end_time = pd.Timestamp(msg_time) + pd.Timedelta(hours=horizon_hours)
    end_idx = int(times.searchsorted(end_time, side="right"))
    end_idx = min(max(end_idx, start_idx + 1), len(rates))
    exit_price = float(rates.iloc[end_idx - 1]["close"])
    status = "end"

    for idx in range(start_idx, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        if _sl_hit(signal.side, sl, high, low):
            exit_price = sl
            status = "loss"
            break
        if _level_hit(signal.side, tp, high, low):
            exit_price = tp
            status = "win"
            break

    return {
        "status": status,
        "entry": entry,
        "exit": exit_price,
        "profit_by_lot": {str(lot): _profit(symbol, signal.side, lot, entry, exit_price) for lot in LOTS},
    }


def _summarize_target(stats: TargetStats) -> dict[str, Any]:
    return {
        "eligible": stats.eligible,
        "wins": stats.wins,
        "losses": stats.losses,
        "end_closes": stats.end_closes,
        "no_entry": stats.no_entry,
        "win_rate_pct": round((stats.wins / stats.eligible) * 100.0, 2) if stats.eligible else 0.0,
        "profit_by_lot": {lot: round(value, 2) for lot, value in stats.profit_by_lot.items()},
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=92)
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--max-messages-per-dialog", type=int, default=12000)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=args.days)
    session_path = str((cfg.data_dir / cfg.telegram_session_name).resolve())

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        info = mt5.symbol_info(symbol)
        point = float(getattr(info, "point", 0.01) or 0.01)
        digits = int(getattr(info, "digits", 2) or 2)
        rates = _rates(symbol, args.timeframe, cutoff, now, args.horizon_hours)
        atr_frame = _atr_rates(rates, args.timeframe)

        client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError(f"Telegram session is not authorized: {session_path}")
            dialogs = await _resolve_dialogs(client, cfg.telegram_watch_channels)
            channels: list[ChannelStats] = []
            for index, dialog in enumerate(dialogs, start=1):
                channel = ChannelStats(
                    token=_dialog_token(dialog),
                    title=str(getattr(dialog, "title", "") or ""),
                    username=str(getattr(dialog.entity, "username", "") or ""),
                    dialog_id=int(getattr(dialog, "id", 0) or 0),
                )
                print(f"scan {index}/{len(dialogs)} {_safe_console(channel.title or channel.token)}", flush=True)
                async for message in client.iter_messages(dialog.entity):
                    if message.date and message.date < cutoff:
                        break
                    channel.scanned += 1
                    if channel.scanned > args.max_messages_per_dialog:
                        break
                    text = str(getattr(message, "raw_text", "") or "")
                    if not text:
                        continue
                    if XAU_RE.search(text):
                        channel.xau_mentions += 1
                    parsed = _parse_signal(
                        text,
                        f"{channel.dialog_id}:{int(getattr(message, 'id', 0) or 0)}",
                        channel.dialog_id,
                        channel.title or channel.username or channel.token,
                        str(getattr(message, "post_author", "") or ""),
                        int(getattr(message, "id", 0) or 0),
                    )
                    if parsed is None:
                        continue
                    channel.parsed += 1
                    for target in TARGETS:
                        result = _simulate_target(
                            symbol,
                            parsed,
                            message.date,
                            rates,
                            atr_frame,
                            point,
                            digits,
                            target,
                            args.horizon_hours,
                        )
                        if result["status"] in {"missing_tp", "no_rates"}:
                            continue
                        target_stats = channel.targets[str(target)]
                        if result["status"] == "no_entry":
                            target_stats.no_entry += 1
                            continue
                        target_stats.eligible += 1
                        if result["status"] == "win":
                            target_stats.wins += 1
                        elif result["status"] == "loss":
                            target_stats.losses += 1
                        else:
                            target_stats.end_closes += 1
                        for lot, profit in result["profit_by_lot"].items():
                            target_stats.profit_by_lot[lot] += float(profit)
                channels.append(channel)
        finally:
            await client.disconnect()

        totals = {str(target): TargetStats() for target in TARGETS}
        for channel in channels:
            for target in TARGETS:
                total = totals[str(target)]
                stats = channel.targets[str(target)]
                total.eligible += stats.eligible
                total.wins += stats.wins
                total.losses += stats.losses
                total.end_closes += stats.end_closes
                total.no_entry += stats.no_entry
                for lot, value in stats.profit_by_lot.items():
                    total.profit_by_lot[lot] += value

        report = {
            "generated_utc": now.isoformat(),
            "start_utc": cutoff.isoformat(),
            "end_utc": now.isoformat(),
            "days": args.days,
            "horizon_hours": args.horizon_hours,
            "symbol": cfg.symbol,
            "resolved_symbol": symbol,
            "timeframe": args.timeframe,
            "rates_start_utc": rates["time"].min().isoformat() if not rates.empty else "",
            "rates_end_utc": rates["time"].max().isoformat() if not rates.empty else "",
            "lots": list(LOTS),
            "method": "Full position exits at selected TP. If SL and TP are in the same M1 candle, SL is counted first. If neither hits in horizon, position closes at horizon close. Signals without explicit SL use bot-like ATR auto SL.",
            "totals": {target: _summarize_target(stats) for target, stats in totals.items()},
            "channels": [
                {
                    "token": channel.token,
                    "title": channel.title,
                    "username": channel.username,
                    "dialog_id": channel.dialog_id,
                    "scanned": channel.scanned,
                    "xau_mentions": channel.xau_mentions,
                    "parsed": channel.parsed,
                    "targets": {target: _summarize_target(stats) for target, stats in channel.targets.items()},
                }
                for channel in channels
            ],
        }
        output = Path(args.output) if args.output else cfg.data_dir / "telegram_pnl_backtest_3m.json"
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(output), "totals": report["totals"]}, ensure_ascii=True))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
