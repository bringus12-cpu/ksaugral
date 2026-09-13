from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, shutdown
from app.telegram_signal_bot import (
    _is_cancel_message,
    _is_hold_message,
    _is_phoenix_direction_runner_announcement,
    _parse_signal,
    _phoenix_direction_hint,
    _phoenix_entry_brain,
    _phoenix_levels_plausible_against_market,
    _phoenix_numeric_range,
    _repair_gold_hundred_digit_typo,
    _tp_hit_level,
)
from backtest_phoenix_complete_60d import CHANNEL_ID, CHANNEL_TITLE, _rates


def _candidate_signal(text: str) -> bool:
    normalized = str(text or "").upper()
    has_levels = bool(re.search(r"\b(?:ENTRY|WEJ[SŚ]CIE|SL|STOP\s*LOSS|TP\s*\d*)\b", normalized))
    has_asset = "XAU" in normalized or "GOLD" in normalized
    return has_asset and has_levels


def _outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row.get("status", "")) for row in rows)
    return {
        "positions": len(rows),
        "wins": statuses["win"] + statuses["protected"],
        "losses": statuses["loss"],
        "pnl": round(sum(float(row.get("pnl", 0.0) or 0.0) for row in rows), 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=126)
    parser.add_argument("--backtest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        cutoff = end - timedelta(days=max(2, int(args.days)))
        rates = _rates(symbol, cutoff - timedelta(days=1), end + timedelta(hours=1))
        source = json.loads(Path(args.backtest).read_text(encoding="utf-8"))
        outcomes: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for row in source.get("trades", []):
            outcomes[int(row["message_id"])][str(row["source"])].append(row)

        client = TelegramClient(
            str((cfg.data_dir / "xauusd_signal_bot_backtest_copy.session").resolve()),
            int(cfg.telegram_api_id),
            cfg.telegram_api_hash,
        )
        messages: list[dict[str, Any]] = []
        async with client:
            async for message in client.iter_messages(CHANNEL_ID, offset_date=end, reverse=False):
                message_time = message.date.astimezone(UTC)
                if message_time < cutoff:
                    break
                messages.append({"id": int(message.id), "time": message_time, "text": str(message.message or "")})

        counts: Counter[str] = Counter()
        records: list[dict[str, Any]] = []
        misses: list[dict[str, Any]] = []
        side_hint: str | None = None
        side_hint_time: datetime | None = None
        for row in sorted(messages, key=lambda value: value["time"]):
            text = row["text"]
            counts["messages"] += 1
            if _is_phoenix_direction_runner_announcement(text):
                side_hint = _phoenix_direction_hint(text)
                side_hint_time = row["time"]
                counts["direction_announcements"] += 1
                continue
            if _is_cancel_message(text):
                counts["cancel_updates"] += 1
            if _is_hold_message(text):
                counts["hold_updates"] += 1
            hit_level = _tp_hit_level(text)
            if hit_level > 0:
                counts["tp_hit_updates"] += 1
            numeric_range = _phoenix_numeric_range(text)
            if numeric_range:
                counts["numeric_ranges"] += 1

            fresh_hint = side_hint if side_hint_time and row["time"] - side_hint_time <= timedelta(minutes=20) else None
            signal = _parse_signal(
                text,
                f"audit:{row['id']}",
                CHANNEL_ID,
                CHANNEL_TITLE,
                "",
                int(row["id"]),
                side_hint=fresh_hint,
            )
            if signal is None or signal.asset != "gold" or not signal.entries or not signal.tps:
                if _candidate_signal(text):
                    counts["unparsed_signal_candidates"] += 1
                    misses.append({"message_id": row["id"], "time": row["time"].isoformat(), "reason": "parser_none", "text": text[:600]})
                continue

            counts["parsed_full_signals"] += 1
            candle_time = pd.Timestamp(row["time"]).ceil("5min")
            idx = int(rates["time"].searchsorted(candle_time, side="left"))
            if idx >= len(rates):
                counts["missing_market_bar"] += 1
                continue
            market = float(rates.iloc[idx]["open"])
            repaired = _repair_gold_hundred_digit_typo(signal, market)
            plausible = _phoenix_levels_plausible_against_market(repaired, market)
            if not plausible:
                counts["implausible_after_repair"] += 1
                misses.append({"message_id": row["id"], "time": row["time"].isoformat(), "reason": "implausible_after_repair", "market": market, "signal": repaired.as_dict()})
                continue
            counts["accepted_full_signals"] += 1
            brain = _phoenix_entry_brain(repaired, market, [float(value) for value in repaired.entries])
            module_rows = outcomes.get(int(row["id"]), {})
            module_summary = {name: _outcome_summary(values) for name, values in module_rows.items()}
            records.append(
                {
                    "message_id": int(row["id"]),
                    "time": row["time"].isoformat(),
                    "side": repaired.side,
                    "entries": [float(value) for value in repaired.entries],
                    "sl": float(repaired.sl or 0.0),
                    "tps": [float(value) for value in repaired.tps],
                    "market": round(market, 3),
                    "brain_decision": brain.get("decision"),
                    "zone_state": brain.get("zone_state"),
                    "reached_tp_at_publish": brain.get("reached_level"),
                    "live_tps": brain.get("live_tps"),
                    "modules": module_summary,
                }
            )

        decisions = Counter(str(row.get("brain_decision", "")) for row in records)
        zone_states = Counter(str(row.get("zone_state", "")) for row in records)
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": cutoff.isoformat(), "end": end.isoformat(), "calendar_days": int(args.days)},
            "channel": CHANNEL_TITLE,
            "timeframe": "M5 conservative, SL first on ambiguous candles",
            "counts": dict(counts),
            "brain_decisions": dict(decisions),
            "zone_states": dict(zone_states),
            "parser_capture_pct": round(100.0 * counts["accepted_full_signals"] / max(1, counts["parsed_full_signals"] + counts["unparsed_signal_candidates"]), 2),
            "misses": misses,
            "signals": records,
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: output[key] for key in ("range", "counts", "brain_decisions", "zone_states", "parser_capture_pct")}, indent=2, ensure_ascii=False))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
