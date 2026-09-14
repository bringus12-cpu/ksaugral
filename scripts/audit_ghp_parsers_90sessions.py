from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.ghp_parser import GHP_CHAT_IDS, GhpMessage, parse_ghp_message
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.provider_update_agent import pending_cancel_candidate, review_provider_pending_update


PUBLIC_USERNAME = "ghptrading"
SYMBOL_CANDIDATES = {
    "gold": ("XAUUSD", "GOLD"),
    "btc": ("BTCUSD",),
    "nas100": ("NAS100", "US100", "USTEC", "NDAQ"),
    "us30": ("DJ30", "US30"),
    "ger40": ("GER40", "DE40", "DAX40"),
    "wti": ("WTI", "USOIL", "XTIUSD"),
}


def _resolve_symbol(asset: str) -> str | None:
    profile_map: dict[str, str] = {}
    raw_map = str(os.getenv("SIGNAL_ASSET_SYMBOL_MAP", "") or "").strip()
    if raw_map:
        try:
            profile_map = {str(key).lower(): str(value) for key, value in json.loads(raw_map).items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            profile_map = {}
    mapped = profile_map.get(asset.lower())
    candidates = ((mapped,) if mapped else ()) + SYMBOL_CANDIDATES.get(asset, (asset.upper(),))
    for candidate in candidates:
        try:
            return ensure_symbol(candidate)
        except Exception:
            continue
    return None


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def _touch(side: str, kind: str, entry: float, high: float, low: float) -> bool:
    if kind == "market":
        return True
    if side == "buy":
        return low <= entry if kind == "limit" else high >= entry
    return high >= entry if kind == "limit" else low <= entry


def _reached(side: str, level: float, high: float, low: float) -> bool:
    return high >= level if side == "buy" else low <= level


def _profit(symbol: str, side: str, entry: float, exit_price: float, lot: float = 0.01) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price)
    return float(value or 0.0)


def _signature(row: dict[str, Any]) -> tuple[Any, ...]:
    signal = row["parsed"]["signal"]
    return (
        signal["asset"],
        signal["side"],
        signal["order_kind"],
        tuple(round(value, 3) for value in signal["entries"]),
        round(signal["sl"], 3),
        tuple(round(value, 3) for value in signal["tps"]),
    )


def _deduplicate(rows: list[dict[str, Any]], minutes: int = 20) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    latest: dict[tuple[Any, ...], datetime] = {}
    duplicates = 0
    for row in sorted(rows, key=lambda item: item["date_dt"]):
        signature = _signature(row)
        previous = latest.get(signature)
        if previous and row["date_dt"] - previous <= timedelta(minutes=minutes):
            duplicates += 1
            continue
        latest[signature] = row["date_dt"]
        kept.append(row)
    return kept, duplicates


def _parse_provider_message(text: str, title: str, cancel_agent_enabled: bool) -> GhpMessage:
    parsed = parse_ghp_message(text, title)
    if (
        cancel_agent_enabled
        and parsed.signal is None
        and parsed.kind == "commentary"
        and pending_cancel_candidate(text)
    ):
        return replace(parsed, kind="cancel")
    return parsed


def _attach_actions(rows: list[dict[str, Any]]) -> None:
    signals_by_message: dict[tuple[int, int], dict[str, Any]] = {}
    latest_by_chat_asset: dict[tuple[int, str], dict[str, Any]] = {}
    latest_by_chat: dict[int, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: item["date_dt"]):
        parsed = row["parsed"]
        signal = parsed.get("signal")
        if signal:
            row["actions"] = []
            signals_by_message[(row["chat_id"], row["message_id"])] = row
            latest_by_chat_asset[(row["chat_id"], signal["asset"])] = row
            latest_by_chat[row["chat_id"]] = row
            continue
        if parsed["kind"] not in {"cancel", "close", "close_partial", "breakeven", "tp_hit", "sl_hit"}:
            continue
        target = None
        if row["reply_to"]:
            target = signals_by_message.get((row["chat_id"], row["reply_to"]))
        if target is None and parsed.get("asset"):
            target = latest_by_chat_asset.get((row["chat_id"], parsed["asset"]))
        if target is None:
            target = latest_by_chat.get(row["chat_id"])
        if target is not None and row["date_dt"] - target["date_dt"] <= timedelta(hours=72):
            target.setdefault("actions", []).append(
                {
                    "time": row["date"],
                    "time_dt": row["date_dt"],
                    "kind": parsed["kind"],
                    "fraction": parsed.get("close_fraction", 0.0),
                    "move_to_be": parsed.get("move_to_be", False),
                    "message_id": row["message_id"],
                    "reply_routed": bool(row["reply_to"]),
                    "text": row.get("text", ""),
                }
            )


def _simulate(
    row: dict[str, Any],
    frame: pd.DataFrame,
    symbol: str,
    target_index: int,
    expiry_minutes: int,
    be_after_tp1: bool,
    cancel_agent_enabled: bool = True,
) -> dict[str, Any]:
    signal = row["parsed"]["signal"]
    side = signal["side"]
    entries = signal["entries"]
    entry = sum(entries) / len(entries)
    sl = float(signal["sl"])
    tps = signal["tps"]
    target = float(tps[min(target_index, len(tps)) - 1])
    risk_distance = abs(entry - sl)
    if risk_distance <= 0.0:
        return {"status": "invalid", "r": 0.0, "pnl_001": 0.0, "entry": entry, "sl": sl}
    start_idx = int(frame["time"].searchsorted(pd.Timestamp(row["date_dt"]), side="left"))
    if start_idx >= len(frame):
        return {"status": "no_rates", "r": 0.0, "pnl_001": 0.0, "entry": entry, "sl": sl}
    expiry_at = row["date_dt"] + timedelta(minutes=expiry_minutes)
    end_at = row["date_dt"] + timedelta(hours=72)
    end_idx = min(len(frame), int(frame["time"].searchsorted(pd.Timestamp(end_at), side="right")))
    trigger_idx = start_idx
    if signal["order_kind"] != "market":
        trigger_idx = -1
        expiry_idx = min(end_idx, int(frame["time"].searchsorted(pd.Timestamp(expiry_at), side="right")))
        for idx in range(start_idx, expiry_idx):
            bar = frame.iloc[idx]
            if _touch(side, signal["order_kind"], entry, float(bar.high), float(bar.low)):
                trigger_idx = idx
                break
        if trigger_idx < 0:
            return {"status": "expired", "r": 0.0, "pnl_001": 0.0, "entry": entry, "sl": sl}

    opened_at = frame.iloc[trigger_idx].time.to_pydatetime()

    actions = sorted(row.get("actions", []), key=lambda item: item["time_dt"])
    action_idx = 0
    current_sl = sl
    fraction = 1.0
    realized_r = 0.0
    realized_pnl = 0.0
    tp1_seen = False
    for idx in range(trigger_idx, end_idx):
        bar = frame.iloc[idx]
        bar_time = bar.time.to_pydatetime()
        while action_idx < len(actions) and actions[action_idx]["time_dt"] <= bar_time:
            action = actions[action_idx]
            action_idx += 1
            if action["kind"] == "cancel" and idx <= trigger_idx:
                if not cancel_agent_enabled:
                    return {"status": "cancelled", "r": 0.0, "pnl_001": 0.0, "entry": entry, "sl": sl}
                cancel_review = review_provider_pending_update(
                    text=str(action.get("text", "") or ""),
                    scoped=True,
                    side=side,
                    asset=str(signal.get("asset", "") or ""),
                    entry=entry,
                    tp1=float(tps[0]),
                    current_price=float(bar.open),
                    created_utc=row["date_dt"].isoformat(),
                )
                if cancel_review.decision == "cancel":
                    return {"status": "cancelled", "r": 0.0, "pnl_001": 0.0, "entry": entry, "sl": sl}
            if action["kind"] == "breakeven":
                current_sl = entry
            elif action["kind"] == "close_partial" and fraction > 0.0:
                close_fraction = min(fraction, max(0.0, float(action["fraction"] or 0.5)))
                close_price = float(bar.open)
                leg_r = ((close_price - entry) if side == "buy" else (entry - close_price)) / risk_distance
                realized_r += close_fraction * leg_r
                realized_pnl += close_fraction * _profit(symbol, side, entry, close_price)
                fraction -= close_fraction
                if action["move_to_be"]:
                    current_sl = entry
            elif action["kind"] == "close" and fraction > 0.0:
                close_price = float(bar.open)
                leg_r = ((close_price - entry) if side == "buy" else (entry - close_price)) / risk_distance
                realized_r += fraction * leg_r
                realized_pnl += fraction * _profit(symbol, side, entry, close_price)
                return {"status": "channel_close", "r": realized_r, "pnl_001": realized_pnl, "entry": entry, "sl": sl, "opened": opened_at, "closed": bar_time}

        high, low = float(bar.high), float(bar.low)
        sl_hit = low <= current_sl if side == "buy" else high >= current_sl
        tp_hit = _reached(side, target, high, low)
        tp1_hit = _reached(side, float(tps[0]), high, low)
        if sl_hit and (tp_hit or tp1_hit):
            tp_hit = tp1_hit = False
        if sl_hit:
            leg_r = ((current_sl - entry) if side == "buy" else (entry - current_sl)) / risk_distance
            realized_r += fraction * leg_r
            realized_pnl += fraction * _profit(symbol, side, entry, current_sl)
            return {"status": "be" if abs(current_sl - entry) < 1e-9 else "sl", "r": realized_r, "pnl_001": realized_pnl, "entry": entry, "sl": sl, "opened": opened_at, "closed": bar_time}
        if tp_hit:
            leg_r = abs(target - entry) / risk_distance
            realized_r += fraction * leg_r
            realized_pnl += fraction * _profit(symbol, side, entry, target)
            return {"status": f"tp{min(target_index, len(tps))}", "r": realized_r, "pnl_001": realized_pnl, "entry": entry, "sl": sl, "opened": opened_at, "closed": bar_time}
        if tp1_hit and not tp1_seen:
            tp1_seen = True
            if be_after_tp1 and target_index > 1:
                current_sl = entry
    close_price = float(frame.iloc[max(trigger_idx, end_idx - 1)].close)
    leg_r = ((close_price - entry) if side == "buy" else (entry - close_price)) / risk_distance
    realized_r += fraction * leg_r
    realized_pnl += fraction * _profit(symbol, side, entry, close_price)
    return {"status": "timeout", "r": realized_r, "pnl_001": realized_pnl, "entry": entry, "sl": sl, "opened": opened_at, "closed": frame.iloc[max(trigger_idx, end_idx - 1)].time.to_pydatetime()}


async def _messages(
    client: TelegramClient,
    start: datetime,
    cancel_agent_enabled: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    channels: dict[str, Any] = {}
    async for dialog in client.iter_dialogs():
        username = str(getattr(dialog.entity, "username", "") or "").lower()
        if int(dialog.id) not in GHP_CHAT_IDS and username != PUBLIC_USERNAME:
            continue
        channel_key = username or str(dialog.id)
        channels[channel_key] = {"chat_id": int(dialog.id), "title": str(dialog.title or ""), "username": username}
        async for message in client.iter_messages(dialog.entity):
            if message.date and message.date < start:
                break
            text = str(getattr(message, "raw_text", "") or "")
            if not text:
                continue
            parsed = _parse_provider_message(text, str(dialog.title or ""), cancel_agent_enabled)
            row = {
                "channel": channel_key,
                "chat_id": int(dialog.id),
                "title": str(dialog.title or ""),
                "message_id": int(message.id),
                "reply_to": int(getattr(message, "reply_to_msg_id", 0) or 0),
                "date": message.date.isoformat(),
                "date_dt": message.date,
                "edit_date": message.edit_date.isoformat() if message.edit_date else None,
                "edit_delay_seconds": (message.edit_date - message.date).total_seconds() if message.edit_date else 0.0,
                "text": text,
                "parsed": asdict(parsed),
            }
            rows.append(row)
    _attach_actions(rows)
    return rows, channels


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[".env.vantage"])
    parser.add_argument("--session-name", default="channel_parser_audit_20260904")
    parser.add_argument("--sessions", type=int, default=90)
    parser.add_argument("--output", default="data_vantage/ghp_parser_audit_90sessions_20260904.json")
    parser.add_argument("--messages-input", default="")
    parser.add_argument("--legacy-cancel", action="store_true")
    args = parser.parse_args()
    for env_file in args.env:
        load_dotenv(env_file, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        gold = ensure_symbol(cfg.symbol)
        probe = mt5.copy_rates_from_pos(gold, mt5.TIMEFRAME_D1, 0, max(120, args.sessions + 20))
        if probe is None or len(probe) < args.sessions:
            raise RuntimeError("Not enough broker sessions")
        dates = sorted({datetime.fromtimestamp(int(row["time"]), UTC).date() for row in probe})[-args.sessions:]
        start = datetime.combine(dates[0], datetime.min.time(), tzinfo=UTC)
        end = datetime.now(UTC)
        if args.messages_input:
            rows = []
            channels = {}
            with Path(args.messages_input).open("r", encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    row["date_dt"] = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
                    row["parsed"] = asdict(
                        _parse_provider_message(row["text"], row["title"], not args.legacy_cancel)
                    )
                    row.pop("actions", None)
                    if not (start <= row["date_dt"] <= end):
                        continue
                    rows.append(row)
                    channels.setdefault(
                        row["channel"],
                        {
                            "chat_id": int(row["chat_id"]),
                            "title": str(row["title"]),
                            "username": row["channel"] if not str(row["channel"]).startswith("-") else "",
                        },
                    )
            _attach_actions(rows)
        else:
            session_path = str((cfg.data_dir / args.session_name).resolve())
            client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
            await client.connect()
            try:
                if not await client.is_user_authorized():
                    raise RuntimeError("Telegram audit session is not authorized")
                rows, channels = await _messages(client, start, not args.legacy_cancel)
            finally:
                await client.disconnect()

        signal_rows = [row for row in rows if row["parsed"].get("signal")]
        unique_signals, duplicate_count = _deduplicate(signal_rows)
        assets = sorted({row["parsed"]["signal"]["asset"] for row in unique_signals})
        symbols = {asset: _resolve_symbol(asset) for asset in assets}
        rates = {
            asset: _rates(symbol, start - timedelta(hours=4), end + timedelta(hours=73))
            for asset, symbol in symbols.items()
            if symbol
        }
        configurations = {
            "tp1_original_sl_60m": (1, 60, False),
            "tp2_be_after_tp1_60m": (2, 60, True),
            "deep_be_after_tp1_60m": (99, 60, True),
            "tp1_original_sl_15m": (1, 15, False),
            "tp1_original_sl_240m": (1, 240, False),
        }
        results: dict[str, Any] = {}
        for config_name, (target_index, expiry, be_after_tp1) in configurations.items():
            outcomes: list[dict[str, Any]] = []
            for row in unique_signals:
                asset = row["parsed"]["signal"]["asset"]
                symbol = symbols.get(asset)
                frame = rates.get(asset)
                if not symbol or frame is None or frame.empty:
                    continue
                outcome = _simulate(
                    row,
                    frame,
                    symbol,
                    target_index,
                    expiry,
                    be_after_tp1,
                    cancel_agent_enabled=not args.legacy_cancel,
                )
                outcomes.append({"channel": row["channel"], "asset": asset, **outcome})
            decided = [row for row in outcomes if row["status"] not in {"expired", "cancelled", "invalid", "no_rates"}]
            winners = [row for row in decided if row["r"] > 0.0]
            losses = [row for row in decided if row["r"] < 0.0]
            results[config_name] = {
                "signals": len(outcomes),
                "decided": len(decided),
                "wins": len(winners),
                "losses": len(losses),
                "win_rate_pct": round(100.0 * len(winners) / max(1, len(winners) + len(losses)), 2),
                "total_r": round(sum(row["r"] for row in outcomes), 2),
                "pnl_001": round(sum(row["pnl_001"] for row in outcomes), 2),
                "status_counts": dict(Counter(row["status"] for row in outcomes)),
                "by_channel": {
                    channel: {
                        "signals": len(group),
                        "win_rate_pct": round(100.0 * sum(row["r"] > 0 for row in group) / max(1, sum(row["r"] != 0 for row in group)), 2),
                        "total_r": round(sum(row["r"] for row in group), 2),
                        "pnl_001": round(sum(row["pnl_001"] for row in group), 2),
                    }
                    for channel, group in ((key, [row for row in outcomes if row["channel"] == key]) for key in channels)
                },
                "by_asset": {
                    asset: {
                        "signals": len(group),
                        "win_rate_pct": round(100.0 * sum(row["r"] > 0 for row in group) / max(1, sum(row["r"] != 0 for row in group)), 2),
                        "total_r": round(sum(row["r"] for row in group), 2),
                        "pnl_001": round(sum(row["pnl_001"] for row in group), 2),
                    }
                    for asset, group in ((key, [row for row in outcomes if row["asset"] == key]) for key in assets)
                },
            }
        channel_stats = {}
        for channel in channels:
            group = [row for row in rows if row["channel"] == channel]
            edits = [row["edit_delay_seconds"] for row in group if row["edit_date"]]
            kinds = Counter(row["parsed"]["kind"] for row in group)
            channel_stats[channel] = {
                **channels[channel],
                "messages": len(group),
                "edited_messages": len(edits),
                "median_edit_delay_seconds": round(median(edits), 2) if edits else 0.0,
                "edits_over_2m": sum(value > 120 for value in edits),
                "edits_over_15m": sum(value > 900 for value in edits),
                "kinds": dict(kinds),
                "signals_by_asset": dict(Counter(row["parsed"]["signal"]["asset"] for row in group if row["parsed"].get("signal"))),
                "reply_routed_actions": sum(action["reply_routed"] for row in group for action in row.get("actions", [])),
                "fallback_routed_actions": sum(not action["reply_routed"] for row in group for action in row.get("actions", [])),
            }
        report = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": start.isoformat(), "end": end.isoformat(), "broker_sessions": len(dates)},
            "channels": channel_stats,
            "messages": len(rows),
            "parsed_signals_before_cross_channel_dedup": len(signal_rows),
            "unique_signals": len(unique_signals),
            "cross_channel_duplicates": duplicate_count,
            "cancel_agent_enabled": not args.legacy_cancel,
            "message_kind_counts": dict(Counter(row["parsed"]["kind"] for row in rows)),
            "assets": {asset: {"symbol": symbols.get(asset), "m1_bars": len(rates.get(asset, []))} for asset in assets},
            "configurations": results,
            "limitations": [
                "Telegram exposes final edited text and edit_date, not the previous text versions.",
                "SL-first is used when TP and SL occur inside the same M1 candle.",
                "Cross-channel copies with identical levels inside 20 minutes are executed once.",
                "The 0.01-lot PnL uses broker order_calc_profit and excludes unknown historical commission and swap.",
            ],
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        raw_path = output.with_name(output.stem + "_messages.jsonl")
        with raw_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                serializable = {key: value for key, value in row.items() if key != "date_dt"}
                for action in serializable.get("actions", []):
                    action.pop("time_dt", None)
                handle.write(json.dumps(serializable, ensure_ascii=True) + "\n")
        print(json.dumps({
            "output": str(output),
            "messages": len(rows),
            "signals": len(signal_rows),
            "unique": len(unique_signals),
            "duplicates": duplicate_count,
            "assets": report["assets"],
            "configurations": results,
        }, ensure_ascii=True, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
