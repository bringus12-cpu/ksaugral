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
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _parse_signal


XAU_RE = re.compile(r"\b(?:xau\s*/?\s*usd|xauusd|xau|gold)\b", re.I)
PRICE = r"(\d{3,6}(?:\.\d+)?)"
ENTRY_RANGE_RE = re.compile(
    rf"\b(?:entry|entries|enter|price|open|zone|buyzone|sellzone)\b[^\d]{{0,20}}{PRICE}\s*(?:-|–|—|to|/)\s*{PRICE}",
    re.I,
)
VARIANTS = ("lower", "upper", "average", "all3")
TARGETS = (1, 2, 3, 4)
LOTS = (0.01, 0.05, 0.1, 1.0)


@dataclass
class Stats:
    eligible: int = 0
    triggered: int = 0
    no_trigger: int = 0
    missing_tp: int = 0
    invalid: int = 0
    sl: int = 0
    timeout: int = 0
    tp_hits: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    target_results: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            str(target): {
                "eligible": 0,
                "wins": 0,
                "losses": 0,
                "timeouts": 0,
                "profit_by_lot": {str(lot): 0.0 for lot in LOTS},
            }
            for target in TARGETS
        }
    )


@dataclass
class Channel:
    token: str
    title: str
    username: str
    dialog_id: int
    scanned: int = 0
    xau_mentions: int = 0
    parsed: int = 0
    range_entries: int = 0
    variants: dict[str, Stats] = field(default_factory=lambda: {name: Stats() for name in VARIANTS})


def _safe(text: str, limit: int = 90) -> str:
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
    out: list[Any] = []
    seen: set[int] = set()
    async for dialog in client.iter_dialogs():
        variants = _dialog_variants(dialog)
        if any(variants & item for item in wanted):
            dialog_id = int(getattr(dialog, "id", 0) or 0)
            if dialog_id not in seen:
                out.append(dialog)
                seen.add(dialog_id)
    return out


def _timeframe(name: str) -> int:
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
    }[name.upper()]


def _rates(symbol: str, timeframe: str, cutoff: datetime, now: datetime, horizon_hours: int) -> pd.DataFrame:
    tf = _timeframe(timeframe)
    raw = mt5.copy_rates_range(symbol, tf, cutoff - timedelta(days=2), now + timedelta(hours=horizon_hours + 2))
    if raw is None or len(raw) == 0:
        raw = mt5.copy_rates_from_pos(symbol, tf, 0, 200000)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.sort_values("time").reset_index(drop=True)


def _entry_range(text: str) -> tuple[float, float] | None:
    match = ENTRY_RANGE_RE.search(text)
    if not match:
        return None
    a = float(match.group(1))
    b = float(match.group(2))
    return min(a, b), max(a, b)


def _entry_prices(signal: Any, text: str) -> dict[str, list[float]]:
    found = _entry_range(text)
    if found:
        low, high = found
    else:
        low = high = float(signal.entry or 0.0)
    avg = round((low + high) / 2.0, 3)
    return {
        "lower": [low],
        "upper": [high],
        "average": [avg],
        "all3": [low, avg, high],
    }


def _level_hit(side: str, target: float, high: float, low: float) -> bool:
    return high >= target if side == "buy" else low <= target


def _sl_hit(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _touch(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _valid(side: str, entry: float, sl: float, tps: list[float]) -> bool:
    if entry <= 0 or sl <= 0 or not tps:
        return False
    direction = 1 if side == "buy" else -1
    if (entry - sl) * direction <= 0:
        return False
    return any((tp - entry) * direction > 0 for tp in tps)


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price)
    return float(value or 0.0)


def _simulate(entry: float, signal: Any, msg_time: datetime, rates: pd.DataFrame, horizon_hours: int) -> dict[str, Any]:
    if rates.empty:
        return {"status": "no_rates", "tp_hits": [False, False, False, False], "entry": entry, "target_results": {}}
    times = rates["time"]
    timestamp = pd.Timestamp(msg_time)
    if timestamp < times.iloc[0]:
        return {"status": "no_rates", "tp_hits": [False, False, False, False], "entry": entry, "target_results": {}}
    start_idx = int(times.searchsorted(timestamp, side="left"))
    if start_idx >= len(rates):
        return {"status": "no_rates", "tp_hits": [False, False, False, False], "entry": entry, "target_results": {}}

    sl = float(signal.sl or 0.0)
    tps = [float(tp) for tp in signal.tps[:4]]
    if not _valid(signal.side, entry, sl, tps):
        return {"status": "invalid", "tp_hits": [False, False, False, False], "entry": entry, "target_results": {}}

    end_time = timestamp + pd.Timedelta(hours=horizon_hours)
    end_idx = int(times.searchsorted(end_time, side="right"))
    end_idx = min(max(end_idx, start_idx + 1), len(rates))

    triggered = -1
    for idx in range(start_idx, end_idx):
        row = rates.iloc[idx]
        if _touch(entry, float(row["high"]), float(row["low"])):
            triggered = idx
            break
    if triggered < 0:
        return {"status": "no_trigger", "tp_hits": [False, False, False, False], "entry": entry, "target_results": {}}

    tp_hits = [False, False, False, False]
    target_results: dict[str, dict[str, Any]] = {}
    open_targets = {i for i in range(min(4, len(tps)))}
    for idx in range(triggered, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        if _sl_hit(signal.side, sl, high, low):
            for target_idx in list(open_targets):
                target_results[str(target_idx + 1)] = {"status": "loss", "exit": sl}
            return {"status": "sl", "tp_hits": tp_hits, "entry": entry, "target_results": target_results}
        for i, tp in enumerate(tps):
            if not tp_hits[i] and _level_hit(signal.side, tp, high, low):
                tp_hits[i] = True
                if i in open_targets:
                    target_results[str(i + 1)] = {"status": "win", "exit": tp}
                    open_targets.remove(i)
    close_price = float(rates.iloc[end_idx - 1]["close"])
    for target_idx in list(open_targets):
        target_results[str(target_idx + 1)] = {"status": "timeout", "exit": close_price}
    return {"status": "timeout", "tp_hits": tp_hits, "entry": entry, "target_results": target_results}


def _add(stats: Stats, outcome: dict[str, Any], symbol: str, side: str) -> None:
    status = str(outcome["status"])
    if status == "no_rates":
        return
    if status == "invalid":
        stats.invalid += 1
        return
    if status == "no_trigger":
        stats.no_trigger += 1
        stats.eligible += 1
        return
    stats.eligible += 1
    stats.triggered += 1
    if status == "sl":
        stats.sl += 1
    elif status == "timeout":
        stats.timeout += 1
    for i, hit in enumerate(outcome["tp_hits"]):
        if hit:
            stats.tp_hits[i] += 1
    entry = float(outcome.get("entry", 0.0) or 0.0)
    for target, result in dict(outcome.get("target_results", {})).items():
        target_stats = stats.target_results[target]
        target_stats["eligible"] += 1
        result_status = str(result.get("status", ""))
        if result_status == "win":
            target_stats["wins"] += 1
        elif result_status == "loss":
            target_stats["losses"] += 1
        else:
            target_stats["timeouts"] += 1
        exit_price = float(result.get("exit", entry) or entry)
        for lot in LOTS:
            target_stats["profit_by_lot"][str(lot)] += _profit(symbol, side, lot, entry, exit_price)


def _summary(stats: Stats) -> dict[str, Any]:
    base = max(1, stats.triggered)
    target_results = {}
    for target, result in stats.target_results.items():
        eligible = int(result["eligible"])
        target_results[target] = {
            "eligible": eligible,
            "wins": int(result["wins"]),
            "losses": int(result["losses"]),
            "timeouts": int(result["timeouts"]),
            "win_rate_pct": round((int(result["wins"]) / eligible) * 100.0, 2) if eligible else 0.0,
            "profit_by_lot": {lot: round(float(value), 2) for lot, value in result["profit_by_lot"].items()},
        }
    return {
        "eligible": stats.eligible,
        "triggered": stats.triggered,
        "no_trigger": stats.no_trigger,
        "invalid": stats.invalid,
        "sl": stats.sl,
        "timeout": stats.timeout,
        "tp1_pct": round(stats.tp_hits[0] / base * 100.0, 2),
        "tp2_pct": round(stats.tp_hits[1] / base * 100.0, 2),
        "tp3_pct": round(stats.tp_hits[2] / base * 100.0, 2),
        "tp4_pct": round(stats.tp_hits[3] / base * 100.0, 2),
        "tp_hits": stats.tp_hits,
        "target_results": target_results,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--max-messages-per-dialog", type=int, default=15000)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=args.days)
    session_path = str((cfg.data_dir / cfg.telegram_session_name).resolve())

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        rates = _rates(symbol, args.timeframe, cutoff, now, args.horizon_hours)
        client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
        await client.connect()
        try:
            dialogs = await _resolve_dialogs(client, cfg.telegram_watch_channels)
            channels: list[Channel] = []
            for index, dialog in enumerate(dialogs, start=1):
                channel = Channel(
                    token=_dialog_token(dialog),
                    title=str(getattr(dialog, "title", "") or ""),
                    username=str(getattr(dialog.entity, "username", "") or ""),
                    dialog_id=int(getattr(dialog, "id", 0) or 0),
                )
                print(f"scan {index}/{len(dialogs)} {_safe(channel.title or channel.token)}", flush=True)
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
                    if _entry_range(text):
                        channel.range_entries += 1
                    prices = _entry_prices(parsed, text)
                    for variant, entries in prices.items():
                        for entry in entries:
                            _add(channel.variants[variant], _simulate(entry, parsed, message.date, rates, args.horizon_hours), symbol, parsed.side)
                channels.append(channel)
        finally:
            await client.disconnect()

        totals = {variant: Stats() for variant in VARIANTS}
        for channel in channels:
            for variant in VARIANTS:
                dst = totals[variant]
                src = channel.variants[variant]
                dst.eligible += src.eligible
                dst.triggered += src.triggered
                dst.no_trigger += src.no_trigger
                dst.invalid += src.invalid
                dst.sl += src.sl
                dst.timeout += src.timeout
                dst.tp_hits = [a + b for a, b in zip(dst.tp_hits, src.tp_hits)]
                for target in TARGETS:
                    dst_target = dst.target_results[str(target)]
                    src_target = src.target_results[str(target)]
                    dst_target["eligible"] += src_target["eligible"]
                    dst_target["wins"] += src_target["wins"]
                    dst_target["losses"] += src_target["losses"]
                    dst_target["timeouts"] += src_target["timeouts"]
                    for lot, value in src_target["profit_by_lot"].items():
                        dst_target["profit_by_lot"][lot] += value

        report = {
            "generated_utc": now.isoformat(),
            "start_utc": cutoff.isoformat(),
            "end_utc": now.isoformat(),
            "days": args.days,
            "timeframe": args.timeframe,
            "horizon_hours": args.horizon_hours,
            "resolved_symbol": symbol,
            "rates_start_utc": rates["time"].min().isoformat() if not rates.empty else "",
            "rates_end_utc": rates["time"].max().isoformat() if not rates.empty else "",
            "method": "Entry is treated as pending touch of lower, upper, average, or all three entries. TP is counted only after entry touch; same candle SL/TP conflict is SL first.",
            "totals": {variant: _summary(stats) for variant, stats in totals.items()},
            "channels": [
                {
                    "token": channel.token,
                    "title": channel.title,
                    "dialog_id": channel.dialog_id,
                    "scanned": channel.scanned,
                    "xau_mentions": channel.xau_mentions,
                    "parsed": channel.parsed,
                    "range_entries": channel.range_entries,
                    "variants": {variant: _summary(stats) for variant, stats in channel.variants.items()},
                }
                for channel in channels
            ],
        }
        output = Path(args.output) if args.output else cfg.data_dir / "entry_variants_backtest_100d.json"
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(output), "totals": report["totals"]}, ensure_ascii=True))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
