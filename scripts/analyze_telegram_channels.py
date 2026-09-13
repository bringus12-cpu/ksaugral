from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
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
from app.telegram_signal_bot import _looks_like_signal_candidate, _parse_signal, _select_execution_tp


XAU_RE = re.compile(r"\b(?:xau\s*/?\s*usd|xauusd|xau|gold)\b", re.I)
SIDE_RE = re.compile(r"\b(?:buy|buying|long|sell|selling|short)\b", re.I)
LEVEL_RE = re.compile(r"\b(?:tp|take\s*profit|target|targets|tgt|sl|s/l|stop\s*loss|entry|limit|stop)\b", re.I)


@dataclass
class ChannelStats:
    token: str
    title: str
    username: str
    dialog_id: int
    scanned: int = 0
    xau_mentions: int = 0
    candidates: int = 0
    parsed: int = 0
    explicit_sl: int = 0
    simulated: int = 0
    tp_target: int = 0
    tp1_only: int = 0
    sl: int = 0
    timeout: int = 0
    not_triggered: int = 0
    invalid: int = 0
    sample_unparsed: list[str] | None = None


def _candidate(text: str) -> bool:
    return _looks_like_signal_candidate(text)


def _compact(text: str, limit: int = 260) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit]


def _safe_console(text: str, limit: int = 80) -> str:
    return (text or "").encode("ascii", "ignore").decode("ascii")[:limit]


def _token_for_dialog(dialog: Any) -> str:
    username = getattr(dialog.entity, "username", None)
    if username:
        return str(username)
    return str(dialog.id)


def _is_channel_like(dialog: Any) -> bool:
    entity = getattr(dialog, "entity", None)
    return bool(
        getattr(dialog, "is_channel", False)
        or getattr(dialog, "is_group", False)
        or getattr(entity, "broadcast", False)
        or getattr(entity, "megagroup", False)
    )


def _rates(symbol: str, cutoff: datetime, now: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 80000)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    frame = frame[frame["time"] >= pd.Timestamp(cutoff - timedelta(days=3))]
    return frame


def _dir(side: str) -> int:
    return 1 if side == "buy" else -1


def _level_valid(side: str, entry: float, sl: float, tp: float) -> bool:
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False
    direction = _dir(side)
    return (tp - entry) * direction > 0 and (entry - sl) * direction > 0


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


def _simulate(signal: Any, rates: pd.DataFrame, target_index: int, horizon_hours: int) -> str:
    if rates.empty:
        return "no_rates"
    times = rates["time"]
    msg_time = pd.Timestamp(signal.message_time)
    start_idx = int(times.searchsorted(msg_time, side="left"))
    if start_idx >= len(rates):
        return "timeout"

    entry = float(signal.entry or 0.0)
    if signal.order_kind == "market":
        entry = float(rates.iloc[start_idx]["close"])
    execution_tp = _select_execution_tp(signal.tps, target_index)
    tp1 = float(signal.tps[0])
    sl = float(signal.sl or 0.0)
    if not _level_valid(signal.side, entry, sl, execution_tp) or not _level_valid(signal.side, entry, sl, tp1):
        return "invalid"

    end_time = msg_time + pd.Timedelta(hours=horizon_hours)
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
            return "not_triggered"

    tp1_seen = False
    for idx in range(triggered_idx, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        if signal.side == "buy":
            sl_hit = low <= sl
            tp1_hit = high >= tp1
            target_hit = high >= execution_tp
        else:
            sl_hit = high >= sl
            tp1_hit = low <= tp1
            target_hit = low <= execution_tp

        if sl_hit and (target_hit or tp1_hit):
            return "sl"
        if sl_hit:
            return "tp1_only" if tp1_seen else "sl"
        if target_hit:
            return "tp_target"
        if tp1_hit:
            tp1_seen = True

    return "tp1_only" if tp1_seen else "timeout"


def _score(stats: ChannelStats) -> float:
    if stats.parsed == 0:
        return -100.0
    coverage = stats.parsed / max(1, stats.candidates)
    sl_coverage = stats.explicit_sl / max(1, stats.parsed)
    sim_base = max(1, stats.simulated)
    tp_rate = stats.tp_target / sim_base
    tp1_rate = stats.tp1_only / sim_base
    sl_rate = stats.sl / sim_base
    nt_rate = stats.not_triggered / sim_base
    volume_bonus = min(20.0, math.log1p(stats.parsed) * 4.0)
    return round((tp_rate * 100.0) + (tp1_rate * 35.0) + (coverage * 25.0) + (sl_coverage * 15.0) + volume_bonus - (sl_rate * 90.0) - (nt_rate * 20.0), 2)


async def _scan_dialog(
    client: TelegramClient,
    dialog: Any,
    cutoff: datetime,
    rates: pd.DataFrame,
    target_index: int,
    horizon_hours: int,
    max_messages: int,
    simulate: bool,
) -> ChannelStats:
    stats = ChannelStats(
        token=_token_for_dialog(dialog),
        title=str(getattr(dialog, "title", "") or ""),
        username=str(getattr(dialog.entity, "username", "") or ""),
        dialog_id=int(dialog.id),
        sample_unparsed=[],
    )
    async for message in client.iter_messages(dialog.entity):
        if message.date and message.date < cutoff:
            break
        stats.scanned += 1
        if stats.scanned > max_messages:
            break
        text = str(getattr(message, "raw_text", "") or "")
        if not text:
            continue
        if XAU_RE.search(text):
            stats.xau_mentions += 1
        if not _candidate(text):
            continue
        stats.candidates += 1
        uid = f"{dialog.id}:{int(getattr(message, 'id', 0) or 0)}"
        parsed = _parse_signal(
            text,
            uid,
            int(dialog.id),
            stats.title or stats.username or str(dialog.id),
            str(getattr(message, "post_author", "") or ""),
            int(getattr(message, "id", 0) or 0),
        )
        if parsed is None:
            if stats.sample_unparsed is not None and len(stats.sample_unparsed) < 3:
                stats.sample_unparsed.append(_compact(text))
            continue
        stats.parsed += 1
        if parsed.sl > 0:
            stats.explicit_sl += 1
        if simulate:
            setattr(parsed, "message_time", message.date)
            outcome = _simulate(parsed, rates, target_index, horizon_hours)
            if outcome in {"tp_target", "tp1_only", "sl", "timeout", "not_triggered", "invalid"}:
                if outcome != "invalid":
                    stats.simulated += 1
                setattr(stats, outcome, getattr(stats, outcome) + 1)
    return stats


def _name_suggests_gold(dialog: Any) -> bool:
    text = f"{getattr(dialog, 'title', '')} {getattr(dialog.entity, 'username', '')}".upper()
    return "XAU" in text or "GOLD" in text


def _configured_dialog(dialog: Any, configured: set[str]) -> bool:
    token = _token_for_dialog(dialog).lower().lstrip("@")
    username = str(getattr(dialog.entity, "username", "") or "").lower()
    return token in configured or username in configured or str(dialog.id) in configured


def _prefilter_score(sample: ChannelStats, dialog: Any, configured: set[str]) -> int:
    score = sample.parsed * 100 + sample.candidates * 50 + min(sample.xau_mentions, 100)
    if _name_suggests_gold(dialog):
        score += 25
    if _configured_dialog(dialog, configured):
        score += 1000
    return score


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-name", default="channel_discovery")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--sample-messages-per-dialog", type=int, default=300)
    parser.add_argument("--max-messages-per-dialog", type=int, default=6000)
    parser.add_argument("--max-deep-dialogs", type=int, default=30)
    parser.add_argument("--only-tokens", default="")
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    now = datetime.now(UTC)
    session_path = str((cfg.data_dir / args.session_name).resolve())

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        rates = _rates(symbol, cutoff, now)
    finally:
        shutdown()

    report: dict[str, Any] = {
        "generated_utc": now.isoformat(),
        "days": args.days,
        "sample_messages_per_dialog": args.sample_messages_per_dialog,
        "max_messages_per_dialog": args.max_messages_per_dialog,
        "max_deep_dialogs": args.max_deep_dialogs,
        "only_tokens": args.only_tokens,
        "horizon_hours": args.horizon_hours,
        "symbol": cfg.symbol,
        "resolved_symbol": symbol,
        "channels": [],
        "recommended_watch_channels": [],
    }

    client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram analysis session is not authorized")
        dialogs = [dialog async for dialog in client.iter_dialogs() if _is_channel_like(dialog)]
        configured = {item.lower().lstrip("@") for item in cfg.telegram_watch_channels}
        only_tokens = {item.strip().lower().lstrip("@") for item in args.only_tokens.split(",") if item.strip()}
        if only_tokens:
            deep_dialogs = [
                dialog
                for dialog in dialogs
                if _token_for_dialog(dialog).lower().lstrip("@") in only_tokens
                or str(getattr(dialog.entity, "username", "") or "").lower() in only_tokens
                or str(dialog.id) in only_tokens
            ]
        else:
            deep_candidates = []
            for dialog in dialogs:
                try:
                    sample = await _scan_dialog(
                        client,
                        dialog,
                        cutoff,
                        rates,
                        cfg.signal_tp_target_index,
                        args.horizon_hours,
                        args.sample_messages_per_dialog,
                        simulate=False,
                    )
                except Exception as exc:
                    print(f"prefilter skip: {_safe_console(str(getattr(dialog, 'title', '')))} {type(exc).__name__}", flush=True)
                    continue
                if sample.candidates or sample.xau_mentions >= 2 or _name_suggests_gold(dialog) or _configured_dialog(dialog, configured):
                    deep_candidates.append((_prefilter_score(sample, dialog, configured), dialog))

            deep_candidates.sort(key=lambda item: item[0], reverse=True)
            deep_dialogs = [dialog for _, dialog in deep_candidates[: max(1, args.max_deep_dialogs)]]
        print(f"prefiltered {len(deep_dialogs)} of {len(dialogs)} channel/group dialogs", flush=True)
        for index, dialog in enumerate(deep_dialogs, start=1):
            try:
                stats = await _scan_dialog(
                    client,
                    dialog,
                    cutoff,
                    rates,
                    cfg.signal_tp_target_index,
                    args.horizon_hours,
                    args.max_messages_per_dialog,
                    simulate=True,
                )
            except Exception as exc:
                print(f"deep skip {index}/{len(deep_dialogs)}: {_safe_console(str(getattr(dialog, 'title', '')))} {type(exc).__name__}", flush=True)
                continue
            print(f"deep {index}/{len(deep_dialogs)}: {_safe_console(stats.title)} parsed={stats.parsed} candidates={stats.candidates}", flush=True)
            if stats.candidates or stats.xau_mentions:
                item = asdict(stats)
                item["score"] = _score(stats)
                item["parse_rate"] = round(stats.parsed / max(1, stats.candidates), 4)
                item["explicit_sl_rate"] = round(stats.explicit_sl / max(1, stats.parsed), 4)
                item["tp_target_rate"] = round(stats.tp_target / max(1, stats.simulated), 4)
                item["tp1_or_target_rate"] = round((stats.tp_target + stats.tp1_only) / max(1, stats.simulated), 4)
                item["sl_rate"] = round(stats.sl / max(1, stats.simulated), 4)
                report["channels"].append(item)
    finally:
        await client.disconnect()

    channels = sorted(report["channels"], key=lambda item: (item["score"], item["parsed"]), reverse=True)
    report["channels"] = channels
    recommended = [
        item["token"]
        for item in channels
        if item["parsed"] >= 5 and item["parse_rate"] >= 0.35 and item["explicit_sl_rate"] >= 0.50 and item["score"] > 0
    ]
    report["recommended_watch_channels"] = recommended[:12]

    output = Path(args.output) if args.output else cfg.data_dir / "channel_analysis_report.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

    print(json.dumps({
        "output": str(output),
        "dialogs_scanned": len(report["channels"]),
        "recommended": report["recommended_watch_channels"],
        "top": [
            {
                "token": item["token"],
                "title": item["title"],
                "parsed": item["parsed"],
                "simulated": item["simulated"],
                "tp_target_rate": item["tp_target_rate"],
                "tp1_or_target_rate": item["tp1_or_target_rate"],
                "sl_rate": item["sl_rate"],
                "score": item["score"],
            }
            for item in channels[:10]
        ],
    }, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
