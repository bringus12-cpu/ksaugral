from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SUCCESS_RETCODES = {10008, 10009, 10010}


def _event_time(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp_utc") or event.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _signal(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("signal", "parsed_signal", "edited_signal"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _channel_identity(event: dict[str, Any]) -> tuple[str, str, int]:
    signal = _signal(event)
    chat_id = int(signal.get("chat_id", event.get("chat_id", 0)) or 0)
    title = str(signal.get("chat_title", event.get("chat_title", "")) or "").strip()
    key = str(chat_id) if chat_id else title.lower()
    return key or "unknown", title or str(chat_id or "unknown"), chat_id


def _learning_key(chat_id: int, title: str) -> str:
    lowered = str(title or "").strip().lower()
    if chat_id == -1002864291293 or "phoenix" in lowered:
        return "phoenixvip"
    if chat_id == -1001704634655 or "goldhunter" in lowered or "gold hunter" in lowered:
        return "goldhunterfx"
    if chat_id == -1001914224843 or "xauusd gold signal" in lowered:
        return "xaugoldsign"
    return str(chat_id or lowered or "unknown")


def _read_events(path: Path, cutoff: datetime) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        timestamp = _event_time(event)
        if timestamp is not None and timestamp >= cutoff:
            events.append(event)
    return events


def build_channel_report(
    events_path: Path,
    runtime_state: dict[str, Any],
    watched_channels: tuple[str, ...] | list[str],
    *,
    window_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=max(1, int(window_days)))
    rows: dict[str, dict[str, Any]] = {}

    def row_for(key: str, title: str, chat_id: int) -> dict[str, Any]:
        row = rows.setdefault(
            key,
            {
                "key": key,
                "channel": title,
                "chat_id": chat_id,
                "recognized_ids": set(),
                "unparsed_ids": set(),
                "executed_ids": set(),
                "skipped_ids": set(),
                "order_attempts": 0,
                "successful_orders": 0,
                "skip_reasons": Counter(),
                "wins": 0,
                "losses": 0,
                "be": 0,
                "pnl": 0.0,
            },
        )
        if title and (not row["channel"] or row["channel"] == key):
            row["channel"] = title
        return row

    for event in _read_events(events_path, cutoff):
        event_type = str(event.get("type", "") or "")
        key, title, chat_id = _channel_identity(event)
        if key == "unknown" and event_type not in {"unparsed_signal_candidate"}:
            continue
        row = row_for(key, title, chat_id)
        signal = _signal(event)
        message_id = int(signal.get("message_id", event.get("message_id", 0)) or 0)
        message_key = f"{chat_id}:{message_id}" if message_id else ""
        if event_type == "signal" and message_key:
            row["recognized_ids"].add(message_key)
        elif event_type == "unparsed_signal_candidate" and message_key:
            row["unparsed_ids"].add(message_key)
        elif event_type == "order_attempt":
            row["order_attempts"] += 1
            if int(event.get("retcode", 0) or 0) in SUCCESS_RETCODES:
                row["successful_orders"] += 1
                if message_key:
                    row["executed_ids"].add(message_key)
        elif event_type == "skip":
            if message_key:
                row["skipped_ids"].add(message_key)
            row["skip_reasons"][str(event.get("reason", "unknown") or "unknown")] += 1

    adaptive = runtime_state.get("adaptive_learning", {}) if isinstance(runtime_state, dict) else {}
    closed = adaptive.get("closed_positions", {}) if isinstance(adaptive, dict) else {}
    by_learning_key: dict[str, dict[str, Any]] = {}
    for row in rows.values():
        by_learning_key[_learning_key(int(row["chat_id"] or 0), str(row["channel"]))] = row
    if isinstance(closed, dict):
        for item in closed.values():
            if not isinstance(item, dict):
                continue
            try:
                closed_at = datetime.fromisoformat(str(item.get("closed_utc", "")).replace("Z", "+00:00"))
            except Exception:
                continue
            if closed_at < cutoff:
                continue
            learning_key = str(item.get("channel", "unknown") or "unknown")
            row = by_learning_key.get(learning_key) or row_for(f"learning:{learning_key}", learning_key, 0)
            outcome = str(item.get("outcome", "") or "")
            if outcome in {"win", "loss", "be"}:
                row[{"win": "wins", "loss": "losses", "be": "be"}[outcome]] += 1
            row["pnl"] += float(item.get("profit", 0.0) or 0.0)

    serialized: list[dict[str, Any]] = []
    for row in rows.values():
        recognized = len(row.pop("recognized_ids"))
        unparsed = len(row.pop("unparsed_ids"))
        executed = len(row.pop("executed_ids"))
        skipped = len(row.pop("skipped_ids"))
        row["recognized"] = recognized
        row["unparsed"] = unparsed
        row["executed_signals"] = executed
        row["skipped_signals"] = skipped
        row["parse_rate_pct"] = round(100.0 * recognized / max(1, recognized + unparsed), 2)
        row["execution_rate_pct"] = round(100.0 * executed / max(1, recognized), 2)
        row["non_loss_rate_pct"] = round(
            100.0 * (int(row["wins"]) + int(row["be"])) / max(1, int(row["wins"]) + int(row["losses"]) + int(row["be"])),
            2,
        )
        row["pnl"] = round(float(row["pnl"]), 2)
        row["skip_reasons"] = dict(row["skip_reasons"].most_common())
        serialized.append(row)

    serialized.sort(key=lambda item: (int(item["recognized"]), int(item["successful_orders"])), reverse=True)
    configured_channels = [str(token).strip() for token in watched_channels if str(token or "").strip()]
    return {
        "generated_utc": now.isoformat(),
        "window_days": int(window_days),
        "configured_count": len(configured_channels),
        "configured_channels": configured_channels,
        "channels": serialized,
        "totals": {
            "recognized": sum(int(item["recognized"]) for item in serialized),
            "unparsed": sum(int(item["unparsed"]) for item in serialized),
            "executed_signals": sum(int(item["executed_signals"]) for item in serialized),
            "successful_orders": sum(int(item["successful_orders"]) for item in serialized),
            "wins": sum(int(item["wins"]) for item in serialized),
            "losses": sum(int(item["losses"]) for item in serialized),
            "be": sum(int(item["be"]) for item in serialized),
            "pnl": round(sum(float(item["pnl"]) for item in serialized), 2),
        },
    }


def write_channel_report(
    output_path: Path,
    events_path: Path,
    runtime_state: dict[str, Any],
    watched_channels: tuple[str, ...] | list[str],
    *,
    window_days: int = 7,
) -> dict[str, Any]:
    report = build_channel_report(events_path, runtime_state, watched_channels, window_days=window_days)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    return report
