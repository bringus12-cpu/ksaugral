from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.telegram_signal_bot import _parse_signal


def load_events(path: Path, start: datetime) -> dict[tuple[int, int], list[dict]]:
    rows: dict[tuple[int, int], list[dict]] = defaultdict(list)
    if not path.exists():
        return rows
    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(str(row.get("timestamp_utc", "")).replace("Z", "+00:00"))
        except (ValueError, json.JSONDecodeError):
            continue
        if when < start:
            continue
        signal = row.get("signal") or {}
        chat_id = int(signal.get("chat_id") or row.get("chat_id") or 0)
        message_id = int(signal.get("message_id") or row.get("message_id") or 0)
        if chat_id and message_id:
            rows[(chat_id, message_id)].append(row)
    return rows


def looks_signal_like(text: str) -> bool:
    value = text.lower()
    return bool(
        re.search(r"\b(buy|sell|entry|sl|stop\s*loss|tp\s*\d*|xau|gold|nas\s*100|us\s*30)\b", value)
        or "wrzucę teraz" in value
        or "wrzuce teraz" in value
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-09-04T00:00:00+00:00")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))

    load_dotenv(ROOT / ".env", override=True)
    load_dotenv(ROOT / ".env.vantage", override=True)
    cfg = load_settings()
    tokens = {token.strip().lower() for token in os.getenv("WATCH_CHANNELS", "").split(",") if token.strip()}
    event_sources = {
        "vantage": load_events(ROOT / "data_vantage/telegram_signal_events.jsonl", start),
        "puprime": load_events(ROOT / "data_puprime_live/signal/telegram_signal_events.jsonl", start),
    }
    session = cfg.data_dir / "xauusd_signal_bot_backtest_copy"
    client = TelegramClient(str(session.resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        dialogs = []
        async for dialog in client.iter_dialogs():
            candidates = {
                str(dialog.id).lower(),
                str(getattr(dialog.entity, "username", "") or "").lower(),
                str(dialog.title or "").lower(),
            }
            if tokens.intersection(candidates):
                dialogs.append(dialog)

        messages = []
        channel_summary = {}
        for dialog in dialogs:
            counters = Counter()
            async for message in client.iter_messages(dialog.entity):
                if message.date < start:
                    break
                text = str(message.raw_text or "").strip()
                if not text:
                    continue
                counters["messages"] += 1
                signal = _parse_signal(
                    text,
                    f"{dialog.id}:{message.id}",
                    int(dialog.id),
                    str(dialog.title or ""),
                    str(getattr(message, "post_author", "") or ""),
                    int(message.id),
                )
                if signal is not None:
                    counters["parsed_signals"] += 1
                elif looks_signal_like(text):
                    counters["unparsed_candidates"] += 1
                traces = {}
                for account, index in event_sources.items():
                    events = index.get((int(dialog.id), int(message.id)), [])
                    attempts = [row for row in events if row.get("type") == "order_attempt"]
                    traces[account] = {
                        "event_types": dict(Counter(str(row.get("type")) for row in events)),
                        "successful_orders": sum(int(row.get("retcode") or 0) in {10008, 10009, 10010} for row in attempts),
                        "failed_order_retcodes": [int(row.get("retcode") or 0) for row in attempts if int(row.get("retcode") or 0) not in {10008, 10009, 10010}],
                        "skip_reasons": [str(row.get("reason")) for row in events if row.get("type") == "skip"],
                        "errors": [str(row.get("error")) for row in events if row.get("error")],
                    }
                if signal is not None or looks_signal_like(text):
                    messages.append(
                        {
                            "date_utc": message.date.isoformat(),
                            "chat_id": int(dialog.id),
                            "chat_title": str(dialog.title or ""),
                            "message_id": int(message.id),
                            "edited": bool(message.edit_date),
                            "text": text,
                            "parsed": signal is not None,
                            "signal": None if signal is None else {
                                "side": signal.side,
                                "asset": signal.asset,
                                "entries": signal.entries,
                                "sl": signal.sl,
                                "tps": signal.tps,
                            },
                            "execution": traces,
                        }
                    )
            channel_summary[str(dialog.id)] = {"title": str(dialog.title or ""), **dict(counters)}

        payload = {
            "start_utc": start.isoformat(),
            "channels": channel_summary,
            "messages": sorted(messages, key=lambda row: row["date_utc"]),
        }
        target = ROOT / args.output
        target.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        print(target)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
