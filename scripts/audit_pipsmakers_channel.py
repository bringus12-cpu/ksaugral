from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import ParsedSignal, _parse_signal


USERNAME = "PipsMakersPL"
EXCLUDED_POST_MARKERS = (
    "profit summary",
    "weekly summary",
    "daily summary",
    "missed entry",
    "cancel entry",
    "signals result",
)
STOP_RE = re.compile(r"\b(?:stop(?:\s*loss)?|sl)\s*[:=🔴-]*\s*(\d{3,5}(?:\.\d+)?)", re.I)


def _parse_candidate(text: str, message_id: int, chat_id: int, title: str) -> tuple[ParsedSignal | None, str]:
    lowered = text.lower()
    if any(marker in lowered for marker in EXCLUDED_POST_MARKERS):
        return None, "summary_or_cancelled_history"
    signal = _parse_signal(text, f"{chat_id}:{message_id}", chat_id, title, "", message_id)
    if signal is None:
        return None, "not_signal"
    if signal.asset != "gold":
        return None, "unsupported_asset"
    if float(signal.sl or 0.0) <= 0.0:
        match = STOP_RE.search(text)
        if match:
            signal = replace(signal, sl=float(match.group(1)))
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
    tps = [float(value) for value in signal.tps if float(value or 0.0) > 0.0]
    if not entries or float(signal.sl or 0.0) <= 0.0 or not tps:
        return None, "missing_levels"
    if signal.side == "buy":
        if float(signal.sl) >= min(entries) or not all(tp > min(entries) for tp in tps):
            return None, "invalid_geometry"
    else:
        if float(signal.sl) <= max(entries) or not all(tp < max(entries) for tp in tps):
            return None, "invalid_geometry"
    return signal, "valid"


def _signature(signal: ParsedSignal) -> tuple[Any, ...]:
    return (
        signal.side,
        tuple(round(float(value), 2) for value in signal.entries),
        round(float(signal.sl), 2),
        tuple(round(float(value), 2) for value in signal.tps),
    )


async def _fetch_messages(cfg, since: datetime, cache: Path, username: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    session = str((cfg.data_dir / "channel_parser_audit_20260904").resolve())
    client = TelegramClient(session, cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        entity = await client.get_entity(username)
        info = {
            "id": int(getattr(entity, "id", 0) or 0),
            "title": str(getattr(entity, "title", "") or ""),
            "username": str(getattr(entity, "username", "") or ""),
        }
        rows: list[dict[str, Any]] = []
        async for message in client.iter_messages(entity):
            if message.date < since:
                break
            text = str(message.raw_text or "")
            if not text:
                continue
            rows.append(
                {
                    "message_id": int(message.id),
                    "date": message.date.isoformat(),
                    "edit_date": message.edit_date.isoformat() if message.edit_date else None,
                    "reply_to": int(getattr(message, "reply_to_msg_id", 0) or 0),
                    "text": text,
                }
            )
    finally:
        await client.disconnect()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return info, rows


def _load_messages(cache: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines() if line.strip()]


def _touch(entry: float, bar: pd.Series, side: str, point: float) -> bool:
    spread = float(bar.get("spread", 0.0) or 0.0) * point
    if side == "buy":
        return float(bar.low) + spread <= entry <= float(bar.high) + spread
    return float(bar.low) <= entry <= float(bar.high)


def _profit(symbol: str, side: str, entry: float, exit_price: float, lot: float = 0.01) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price)
    return float(value or 0.0)


def _simulate_leg(
    frame: pd.DataFrame,
    symbol: str,
    signal: ParsedSignal,
    entry: float,
    expiry_minutes: int,
    target_index: int,
    be_after_tp1: bool,
    point: float,
) -> dict[str, Any]:
    start = datetime.fromisoformat(signal.uid.split("|", 1)[1])
    start_idx = int(frame.time.searchsorted(pd.Timestamp(start), side="left"))
    expiry_idx = min(
        len(frame),
        int(frame.time.searchsorted(pd.Timestamp(start + timedelta(minutes=expiry_minutes)), side="right")),
    )
    trigger_idx = -1
    for idx in range(start_idx, expiry_idx):
        if _touch(entry, frame.iloc[idx], signal.side, point):
            trigger_idx = idx
            break
    if trigger_idx < 0:
        return {"status": "expired", "pnl": 0.0, "r": 0.0}
    target = float(signal.tps[min(max(1, target_index), len(signal.tps)) - 1])
    tp1 = float(signal.tps[0])
    initial_risk = abs(entry - float(signal.sl))
    current_sl = float(signal.sl)
    tp1_seen = False
    end_time = start + timedelta(hours=24)
    end_idx = min(len(frame), int(frame.time.searchsorted(pd.Timestamp(end_time), side="right")))
    for idx in range(trigger_idx, end_idx):
        bar = frame.iloc[idx]
        high, low = float(bar.high), float(bar.low)
        sl_hit = low <= current_sl if signal.side == "buy" else high >= current_sl
        target_hit = high >= target if signal.side == "buy" else low <= target
        tp1_hit = high >= tp1 if signal.side == "buy" else low <= tp1
        if sl_hit and (target_hit or tp1_hit):
            target_hit = tp1_hit = False
        if sl_hit:
            pnl = _profit(symbol, signal.side, entry, current_sl) - 0.06
            return {"status": "be" if current_sl == entry else "sl", "pnl": pnl, "r": pnl / max(0.01, initial_risk)}
        if target_hit:
            pnl = _profit(symbol, signal.side, entry, target) - 0.06
            return {"status": f"tp{min(target_index, len(signal.tps))}", "pnl": pnl, "r": pnl / max(0.01, initial_risk)}
        if tp1_hit and not tp1_seen:
            tp1_seen = True
            if be_after_tp1 and target_index > 1:
                current_sl = entry
    close = float(frame.iloc[max(trigger_idx, end_idx - 1)].close)
    pnl = _profit(symbol, signal.side, entry, close) - 0.06
    return {"status": "timeout", "pnl": pnl, "r": pnl / max(0.01, initial_risk)}


def _summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [float(item["pnl"]) for item in outcomes]
    wins = sum(value > 0.0 for value in pnl)
    losses = sum(value < 0.0 for value in pnl)
    gross_profit = sum(value for value in pnl if value > 0.0)
    gross_loss = abs(sum(value for value in pnl if value < 0.0))
    equity = peak = drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "signals": len(outcomes),
        "triggered_signals": sum(int(item["triggered_legs"] > 0) for item in outcomes),
        "triggered_legs": sum(int(item["triggered_legs"]) for item in outcomes),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "pnl_001_per_leg": round(sum(pnl), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_usd": round(drawdown, 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=USERNAME)
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--cache", default="data_vantage/pipsmakers_60sessions_messages.jsonl")
    parser.add_argument("--messages-input", default="")
    parser.add_argument("--output", default="data_vantage/pipsmakers_60sessions_audit.json")
    args = parser.parse_args()
    load_dotenv(ROOT / args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        probe = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, max(100, args.sessions + 20))
        dates = sorted({datetime.fromtimestamp(int(row["time"]), UTC).date() for row in probe})[-args.sessions:]
        since = datetime.combine(dates[0], datetime.min.time(), tzinfo=UTC)
        cache = ROOT / (args.messages_input or args.cache)
        if args.messages_input:
            info = {"username": args.username}
            rows = _load_messages(cache)
        else:
            info, rows = await _fetch_messages(cfg, since, cache, args.username)
        parsed: list[tuple[datetime, ParsedSignal, int]] = []
        rejection_counts: dict[str, int] = {}
        for row in rows:
            signal, reason = _parse_candidate(
                row["text"],
                int(row["message_id"]),
                int(info.get("id", 0) or 0),
                info.get("title", args.username),
            )
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            if signal:
                stamp = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
                signal = replace(signal, uid=f"{signal.uid}|{stamp.isoformat()}")
                parsed.append((stamp, signal, int(row["message_id"])))
        parsed.sort(key=lambda item: item[0])
        unique: list[tuple[datetime, ParsedSignal, int]] = []
        seen: dict[tuple[Any, ...], datetime] = {}
        duplicate_count = 0
        for item in parsed:
            signature = _signature(item[1])
            if signature in seen and item[0] - seen[signature] <= timedelta(minutes=10):
                duplicate_count += 1
                continue
            seen[signature] = item[0]
            unique.append(item)
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, since - timedelta(hours=1), datetime.now(UTC))
        frame = pd.DataFrame(raw)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        point = float(mt5.symbol_info(symbol).point or 0.01)
        variants = {
            "three_entries_tp1_expiry15": (1, 15, False),
            "three_entries_tp1_expiry60": (1, 60, False),
            "three_entries_tp2_be_after_tp1_expiry15": (2, 15, True),
            "three_entries_tp2_be_after_tp1_expiry60": (2, 60, True),
        }
        results: dict[str, Any] = {}
        details: dict[str, list[dict[str, Any]]] = {}
        for name, (target_index, expiry, use_be) in variants.items():
            outcomes = []
            variant_details = []
            for stamp, signal, message_id in unique:
                leg_results = [
                    _simulate_leg(frame, symbol, signal, float(entry), expiry, target_index, use_be, point)
                    for entry in signal.entries
                ]
                triggered = [leg for leg in leg_results if leg["status"] != "expired"]
                row = {
                    "message_id": message_id,
                    "time_utc": stamp.isoformat(),
                    "side": signal.side,
                    "entries": signal.entries,
                    "sl": signal.sl,
                    "tps": signal.tps,
                    "triggered_legs": len(triggered),
                    "pnl": sum(float(leg["pnl"]) for leg in leg_results),
                    "statuses": [leg["status"] for leg in leg_results],
                }
                outcomes.append(row)
                variant_details.append(row)
            results[name] = _summary(outcomes)
            details[name] = variant_details
        report = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "channel": info,
            "range": {"start": since.isoformat(), "end": datetime.now(UTC).isoformat(), "sessions": args.sessions},
            "messages": len(rows),
            "valid_signal_posts": len(parsed),
            "unique_signals": len(unique),
            "duplicates_removed": duplicate_count,
            "rejections": rejection_counts,
            "assumptions": {
                "lot_per_entry": 0.01,
                "entry_plan": "all published zone levels; two boundaries are expanded by the parser to low/mid/high",
                "commission_per_leg": 0.06,
                "same_bar_ordering": "SL first",
            },
            "variants": results,
            "details": details,
            "limitations": [
                "The channel aggregates multiple providers, so unscoped management messages cannot always be assigned safely.",
                "Telegram exposes the final edited message, not every prior version.",
                "Results use broker M1 candles and do not model tick-level ordering inside a candle.",
            ],
        }
        output = ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("range", "messages", "valid_signal_posts", "unique_signals", "duplicates_removed", "rejections", "variants")}, indent=2, ensure_ascii=False))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
