from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field
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


@dataclass
class ChannelResult:
    token: str
    title: str = ""
    username: str = ""
    dialog_id: int = 0
    scanned: int = 0
    xau_mentions: int = 0
    parsed: int = 0
    simulated: int = 0
    explicit_sl: int = 0
    no_entry: int = 0
    no_rates: int = 0
    not_triggered: int = 0
    stopped: int = 0
    timeout: int = 0
    tp_hits: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    examples: list[dict[str, Any]] = field(default_factory=list)


def _token_variants(token: str) -> set[str]:
    raw = token.strip()
    clean = raw.lower().lstrip("@")
    variants = {clean}
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if clean.startswith(prefix):
            variants.add(clean[len(prefix):])
    if raw.startswith("-100"):
        variants.add(raw[4:])
    if raw.startswith("100"):
        variants.add(raw[3:])
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
    if username:
        return username
    return str(getattr(dialog, "id", "") or "")


def _safe_console(text: str, limit: int = 90) -> str:
    return (text or "").encode("ascii", "ignore").decode("ascii")[:limit]


def _rates(symbol: str, cutoff: datetime, end_at: datetime, horizon_hours: int) -> pd.DataFrame:
    start = cutoff - timedelta(hours=2)
    end = end_at + timedelta(hours=max(1, horizon_hours))
    raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
    if raw is None or len(raw) == 0:
        raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 80000)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.sort_values("time").reset_index(drop=True)


def _level_hit(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _sl_hit(side: str, sl: float, high: float, low: float) -> bool:
    if sl <= 0:
        return False
    return low <= sl if side == "buy" else high >= sl


def _pending_touched(side: str, order_kind: str, entry: float, high: float, low: float) -> bool:
    if order_kind == "market":
        return True
    if side == "buy" and order_kind == "limit":
        return low <= entry
    if side == "sell" and order_kind == "limit":
        return high >= entry
    if side == "buy" and order_kind == "stop":
        return high >= entry
    if side == "sell" and order_kind == "stop":
        return low <= entry
    return False


def _levels_are_valid(side: str, entry: float, tps: list[float], sl: float) -> bool:
    if entry <= 0 or not tps:
        return False
    direction = 1 if side == "buy" else -1
    if any((tp - entry) * direction <= 0 for tp in tps):
        return False
    if sl > 0 and (entry - sl) * direction <= 0:
        return False
    return True


def _simulate(signal: Any, msg_time: datetime, rates: pd.DataFrame, horizon_hours: int) -> dict[str, Any]:
    if rates.empty:
        return {"status": "no_rates", "hit_count": 0, "tp_hits": [False, False, False, False]}
    times = rates["time"]
    start_idx = int(times.searchsorted(pd.Timestamp(msg_time), side="left"))
    if start_idx >= len(rates):
        return {"status": "no_rates", "hit_count": 0, "tp_hits": [False, False, False, False]}

    entry = float(signal.entry or 0.0)
    if entry <= 0:
        entry = float(rates.iloc[start_idx]["close"])
    tps = [float(value) for value in signal.tps[:4]]
    sl = float(signal.sl or 0.0)
    if not _levels_are_valid(signal.side, entry, tps, sl):
        return {"status": "no_entry", "hit_count": 0, "tp_hits": [False, False, False, False]}

    end_time = pd.Timestamp(msg_time) + pd.Timedelta(hours=horizon_hours)
    end_idx = int(times.searchsorted(end_time, side="right"))
    end_idx = min(max(end_idx, start_idx + 1), len(rates))

    triggered_idx = start_idx
    if signal.order_kind != "market":
        triggered_idx = -1
        for idx in range(start_idx, end_idx):
            row = rates.iloc[idx]
            if _pending_touched(signal.side, signal.order_kind, entry, float(row["high"]), float(row["low"])):
                triggered_idx = idx
                break
        if triggered_idx < 0:
            return {"status": "not_triggered", "hit_count": 0, "tp_hits": [False, False, False, False]}

    tp_hits = [False, False, False, False]
    for idx in range(triggered_idx, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        if _sl_hit(signal.side, sl, high, low):
            return {"status": "stopped", "hit_count": sum(tp_hits), "tp_hits": tp_hits}
        for tp_index, tp in enumerate(tps):
            if not tp_hits[tp_index] and _level_hit(signal.side, tp, high, low):
                tp_hits[tp_index] = True
    return {"status": "timeout", "hit_count": sum(tp_hits), "tp_hits": tp_hits}


def _percent(value: int, base: int) -> float:
    return round((value / base) * 100.0, 2) if base else 0.0


async def _resolve_dialogs(client: TelegramClient, tokens: tuple[str, ...]) -> list[Any]:
    token_map = {token: _token_variants(token) for token in tokens}
    dialogs = [dialog async for dialog in client.iter_dialogs()]
    selected: list[Any] = []
    selected_ids: set[int] = set()
    for dialog in dialogs:
        variants = _dialog_variants(dialog)
        if any(variants & wanted for wanted in token_map.values()):
            dialog_id = int(getattr(dialog, "id", 0) or 0)
            if dialog_id not in selected_ids:
                selected.append(dialog)
                selected_ids.add(dialog_id)
    return selected


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-name", default="")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--max-messages-per-dialog", type=int, default=3000)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    now = datetime.now(UTC)
    if args.start_date:
        cutoff = datetime.fromisoformat(args.start_date).replace(tzinfo=UTC)
    else:
        cutoff = now - timedelta(days=args.days)
    if args.end_date:
        end_at = datetime.fromisoformat(args.end_date).replace(tzinfo=UTC) + timedelta(days=1)
    else:
        end_at = now
    session_name = args.session_name or cfg.telegram_session_name
    session_path = str((cfg.data_dir / session_name).resolve())

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        rates = _rates(symbol, cutoff, end_at, args.horizon_hours)
    finally:
        shutdown()

    client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(f"Telegram session is not authorized: {session_path}")
        dialogs = await _resolve_dialogs(client, cfg.telegram_watch_channels)
        results: list[ChannelResult] = []
        for index, dialog in enumerate(dialogs, start=1):
            result = ChannelResult(
                token=_dialog_token(dialog),
                title=str(getattr(dialog, "title", "") or ""),
                username=str(getattr(dialog.entity, "username", "") or ""),
                dialog_id=int(getattr(dialog, "id", 0) or 0),
            )
            print(f"scan {index}/{len(dialogs)} {_safe_console(result.title or result.token)}", flush=True)
            async for message in client.iter_messages(dialog.entity):
                if message.date and message.date < cutoff:
                    break
                if message.date and message.date >= end_at:
                    continue
                result.scanned += 1
                if result.scanned > args.max_messages_per_dialog:
                    break
                text = str(getattr(message, "raw_text", "") or "")
                if not text:
                    continue
                if XAU_RE.search(text):
                    result.xau_mentions += 1
                parsed = _parse_signal(
                    text,
                    f"{result.dialog_id}:{int(getattr(message, 'id', 0) or 0)}",
                    result.dialog_id,
                    result.title or result.username or result.token,
                    str(getattr(message, "post_author", "") or ""),
                    int(getattr(message, "id", 0) or 0),
                )
                if parsed is None:
                    continue
                result.parsed += 1
                if parsed.sl > 0:
                    result.explicit_sl += 1
                outcome = _simulate(parsed, message.date, rates, args.horizon_hours)
                status = str(outcome["status"])
                if status in {"no_entry", "no_rates", "not_triggered"}:
                    setattr(result, status, getattr(result, status) + 1)
                    continue
                result.simulated += 1
                if status == "stopped":
                    result.stopped += 1
                elif status == "timeout":
                    result.timeout += 1
                for tp_index, hit in enumerate(outcome["tp_hits"]):
                    if hit:
                        result.tp_hits[tp_index] += 1
                if len(result.examples) < 5:
                    result.examples.append(
                        {
                            "date": message.date.isoformat() if message.date else "",
                            "side": parsed.side,
                            "entry": parsed.entry,
                            "sl": parsed.sl,
                            "tps": parsed.tps[:4],
                            "status": status,
                            "tp_hits": outcome["tp_hits"],
                        }
                    )
            results.append(result)
    finally:
        await client.disconnect()

    totals = ChannelResult(token="TOTAL", title="TOTAL")
    for result in results:
        for field_name in (
            "scanned",
            "xau_mentions",
            "parsed",
            "simulated",
            "explicit_sl",
            "no_entry",
            "no_rates",
            "not_triggered",
            "stopped",
            "timeout",
        ):
            setattr(totals, field_name, getattr(totals, field_name) + getattr(result, field_name))
        totals.tp_hits = [a + b for a, b in zip(totals.tp_hits, result.tp_hits)]

    def serialize(result: ChannelResult) -> dict[str, Any]:
        payload = asdict(result)
        base = result.simulated
        payload["tp1_pct"] = _percent(result.tp_hits[0], base)
        payload["tp2_pct"] = _percent(result.tp_hits[1], base)
        payload["tp3_pct"] = _percent(result.tp_hits[2], base)
        payload["tp4_pct"] = _percent(result.tp_hits[3], base)
        payload["explicit_sl_pct"] = _percent(result.explicit_sl, result.parsed)
        return payload

    report = {
        "generated_utc": now.isoformat(),
        "days": args.days,
        "start_utc": cutoff.isoformat(),
        "end_utc": end_at.isoformat(),
        "horizon_hours": args.horizon_hours,
        "symbol": cfg.symbol,
        "resolved_symbol": symbol,
        "timeframe": "M1",
        "method": "TP counted only when reached before SL; same M1 candle SL/TP conflict is counted as SL first.",
        "totals": serialize(totals),
        "channels": [serialize(item) for item in results],
    }
    output = Path(args.output) if args.output else cfg.data_dir / "telegram_tp_backtest_30d.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"output": str(output), "totals": report["totals"]}, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
