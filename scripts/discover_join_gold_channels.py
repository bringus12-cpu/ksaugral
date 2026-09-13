from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, UserAlreadyParticipantError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.telegram_signal_bot import _parse_signal


QUERIES = (
    "xauusd signals",
    "gold signals",
    "xau signals",
    "gold forex signals",
    "gold trading signals",
    "xauusd vip signals",
    "gold sniper signals",
    "gold trader signals",
    "xauusd trading",
    "gold scalping signals",
)

NAME_RE = re.compile(r"(?:xau|gold|bullion)", re.I)
SIGNAL_RE = re.compile(r"(?:buy|sell|long|short).*(?:tp|take\s*profit|target).*(?:sl|stop\s*loss)|(?:tp|take\s*profit).*(?:sl|stop\s*loss)", re.I | re.S)


async def inspect_channel(client: TelegramClient, channel: Any, cutoff: datetime, limit: int) -> dict[str, Any] | None:
    title = str(getattr(channel, "title", "") or "")
    username = str(getattr(channel, "username", "") or "")
    if not username or not NAME_RE.search(f"{title} {username}"):
        return None
    scanned = 0
    candidates = 0
    parsed = 0
    newest = None
    samples: list[dict[str, Any]] = []
    try:
        async for message in client.iter_messages(channel, limit=limit):
            if message.date and message.date < cutoff:
                break
            scanned += 1
            newest = newest or message.date
            text = str(getattr(message, "raw_text", "") or "")
            if not SIGNAL_RE.search(text):
                continue
            candidates += 1
            signal = _parse_signal(
                text,
                f"-100{getattr(channel, 'id', '')}:{getattr(message, 'id', '')}",
                -1000000000000 + int(getattr(channel, "id", 0) or 0),
                title,
                "",
                int(getattr(message, "id", 0) or 0),
            )
            if signal:
                parsed += 1
                if len(samples) < 2:
                    samples.append({"id": message.id, "date": message.date.isoformat(), "side": signal.side, "entry": signal.entry, "sl": signal.sl, "tps": signal.tps[:4]})
    except Exception:
        return None
    if candidates < 2 or parsed < 1:
        return None
    return {
        "id": int(getattr(channel, "id", 0) or 0),
        "title": title,
        "username": username,
        "link": f"https://t.me/{username}",
        "participants": int(getattr(channel, "participants_count", 0) or 0),
        "scanned_60d": scanned,
        "candidate_signals_60d": candidates,
        "parsed_signals_60d": parsed,
        "parser_coverage_pct": round(parsed / max(1, candidates) * 100.0, 2),
        "newest_message_utc": newest.isoformat() if newest else None,
        "samples": samples,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--inspect-messages", type=int, default=500)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    source = cfg.data_dir / f"{cfg.telegram_session_name}.session"
    copy = cfg.data_dir / f"{cfg.telegram_session_name}_gold_discovery.session"
    if source.exists():
        shutil.copy2(source, copy)
    client = TelegramClient(str(copy.with_suffix("").resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorized")

    candidates: dict[int, Any] = {}
    for query in QUERIES:
        try:
            result = await client(functions.contacts.SearchRequest(q=query, limit=100))
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            result = await client(functions.contacts.SearchRequest(q=query, limit=100))
        for chat in result.chats:
            if not isinstance(chat, types.Channel):
                continue
            username = str(getattr(chat, "username", "") or "")
            if username and NAME_RE.search(f"{getattr(chat, 'title', '')} {username}"):
                candidates[int(chat.id)] = chat
        await asyncio.sleep(0.7)

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    inspected: list[dict[str, Any]] = []
    for index, channel in enumerate(candidates.values(), start=1):
        result = await inspect_channel(client, channel, cutoff, args.inspect_messages)
        if result:
            inspected.append(result)
            print(f"inspect {index}/{len(candidates)} {result['username']} parsed={result['parsed_signals_60d']}", flush=True)
        await asyncio.sleep(0.15)

    inspected.sort(
        key=lambda item: (
            item["parsed_signals_60d"],
            item["parser_coverage_pct"],
            item["participants"],
        ),
        reverse=True,
    )
    selected = inspected[: args.count]
    joined: list[str] = []
    join_errors: dict[str, str] = {}
    for item in selected:
        username = item["username"]
        try:
            await client(functions.channels.JoinChannelRequest(username))
            joined.append(username)
        except UserAlreadyParticipantError:
            joined.append(username)
        except FloodWaitError as exc:
            join_errors[username] = f"FloodWait {exc.seconds}s"
            break
        except Exception as exc:
            join_errors[username] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(1.2)

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "queries": list(QUERIES),
        "global_candidates": len(candidates),
        "qualified_channels": len(inspected),
        "selected_count": len(selected),
        "joined": joined,
        "join_errors": join_errors,
        "channels": selected,
    }
    path = Path(args.output) if args.output else cfg.data_dir / "discovered_gold_channels_20.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(output, indent=2, ensure_ascii=False))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
