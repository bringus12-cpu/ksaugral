from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _is_cancel_message,
    _is_hold_message,
    _is_phoenix_direction_runner_announcement,
    _is_secure_message,
    _parse_signal,
    _phoenix_direction_hint,
    _phoenix_levels_plausible_against_market,
    _repair_gold_hundred_digit_typo,
    _tp_hit_level,
)
from scripts.backtest_phoenix_active_profile import _completed_sessions
from scripts.backtest_phoenix_complete_60d import CHANNEL_ID, CHANNEL_TITLE, SignalRow, _rates
from scripts.optimize_phoenix_copy_execution import Candidate, _run_candidate, _summary


def _classify(text: str) -> tuple[str, int]:
    level = _tp_hit_level(text)
    if level > 0:
        return "tp_hit", level
    if _is_secure_message(text):
        return "secure_be", 0
    if _is_hold_message(text):
        return "hold", 0
    if _is_cancel_message(text):
        return "close_cancel", 0
    return "", 0


def _summary_values(values: list[float]) -> dict[str, Any]:
    wins = sum(value > 0.0 for value in values)
    losses = sum(value < 0.0 for value in values)
    gross_win = sum(max(0.0, value) for value in values)
    gross_loss = abs(sum(min(0.0, value) for value in values))
    equity = peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "legs": len(values),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "pnl_001": round(sum(values), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_001": round(max_dd, 2),
    }


def _better_stop(side: str, current: float, candidate: float) -> float:
    return max(current, candidate) if side == "buy" else min(current, candidate)


def _managed_replay(
    row: dict[str, Any],
    updates: list[dict[str, Any]],
    rates: pd.DataFrame,
    point: float,
    profit_per_usd_001: float,
    commission: float,
    mode: str,
    signal_tps: list[float],
) -> dict[str, Any]:
    entry_time = pd.Timestamp(row["entry_time"])
    exit_time = pd.Timestamp(row["exit_time"])
    entry_time = entry_time.tz_localize("UTC") if entry_time.tzinfo is None else entry_time.tz_convert("UTC")
    exit_time = exit_time.tz_localize("UTC") if exit_time.tzinfo is None else exit_time.tz_convert("UTC")
    baseline_pnl = float(row["pnl_001"])
    pending_cancel_updates = [
        update
        for update in updates
        if update["kind"] in {"tp_hit", "secure_be", "close_cancel"}
        and update["broker_time"] <= entry_time
    ]
    if pending_cancel_updates:
        return {
            "pnl_001": 0.0,
            "status": "pending_cancelled",
            "baseline_pnl_001": baseline_pnl,
            "delta_001": -baseline_pnl,
        }

    relevant = [
        update
        for update in updates
        if entry_time < update["broker_time"] <= exit_time
    ]
    if not relevant:
        return {
            "pnl_001": baseline_pnl,
            "status": str(row["status"]),
            "baseline_pnl_001": baseline_pnl,
            "delta_001": 0.0,
        }

    side = str(row["side"])
    entry = float(row["entry"])
    target = float(row["tp"])
    current_sl = float(row["sl"])
    start_idx = int(rates["time"].searchsorted(entry_time, side="left"))
    end_idx = min(int(rates["time"].searchsorted(exit_time, side="right")), len(rates))
    update_index = 0
    baseline_exit_idx = max(start_idx, min(end_idx - 1, len(rates) - 1))
    baseline_spread = float(rates.iloc[baseline_exit_idx]["spread"]) * point
    exit_price = float(rates.iloc[baseline_exit_idx]["close"]) + (
        baseline_spread if side == "sell" else 0.0
    )
    status = str(row["status"])
    for index in range(start_idx, end_idx):
        bar_time = pd.Timestamp(rates.iloc[index]["time"])
        while update_index < len(relevant) and relevant[update_index]["broker_time"] <= bar_time:
            update = relevant[update_index]
            if mode != "pending_cancel" and update["kind"] == "close_cancel":
                spread = float(rates.iloc[index]["spread"]) * point
                exit_price = float(rates.iloc[index]["open"]) + (spread if side == "sell" else 0.0)
                status = "provider_close"
                signed = exit_price - entry if side == "buy" else entry - exit_price
                pnl = signed * profit_per_usd_001 - commission
                return {
                    "pnl_001": round(pnl, 4),
                    "status": status,
                    "baseline_pnl_001": baseline_pnl,
                    "delta_001": round(pnl - baseline_pnl, 4),
                }
            if mode != "pending_cancel" and update["kind"] == "secure_be":
                candidate = entry + 0.10 if side == "buy" else entry - 0.10
                current_sl = _better_stop(side, current_sl, candidate)
            if mode == "provider_ladder" and update["kind"] == "tp_hit":
                reached = int(update["level"])
                if reached >= 1:
                    if reached == 1 or not signal_tps:
                        candidate = entry + 0.10 if side == "buy" else entry - 0.10
                    else:
                        candidate = float(signal_tps[min(reached - 2, len(signal_tps) - 1)])
                    current_sl = _better_stop(side, current_sl, candidate)
            update_index += 1

        bar = rates.iloc[index]
        spread = float(bar["spread"]) * point
        high = float(bar["high"])
        low = float(bar["low"])
        exit_high = high if side == "buy" else high + spread
        exit_low = low if side == "buy" else low + spread
        hit_sl = exit_low <= current_sl if side == "buy" else exit_high >= current_sl
        hit_tp = exit_high >= target if side == "buy" else exit_low <= target
        if hit_sl:
            exit_price = current_sl
            status = "managed_stop"
            break
        if hit_tp:
            exit_price = target
            status = "target"
            break
    signed = exit_price - entry if side == "buy" else entry - exit_price
    pnl = signed * profit_per_usd_001 - commission
    return {
        "pnl_001": round(pnl, 4),
        "status": status,
        "baseline_pnl_001": baseline_pnl,
        "delta_001": round(pnl - baseline_pnl, 4),
    }


async def _channel_history(
    cfg: Any,
    cutoff: datetime,
    broker_offset: timedelta,
    rates: pd.DataFrame,
) -> tuple[list[SignalRow], dict[int, list[dict[str, Any]]], dict[str, Any]]:
    raw_messages: list[dict[str, Any]] = []
    client = TelegramClient(
        str((cfg.data_dir / "xauusd_signal_bot_backtest_copy.session").resolve()),
        int(cfg.telegram_api_id),
        cfg.telegram_api_hash,
    )
    async with client:
        async for message in client.iter_messages(CHANNEL_ID, offset_date=datetime.now(UTC), reverse=False):
            message_time = message.date.astimezone(UTC)
            if message_time < cutoff:
                break
            raw_messages.append(
                {
                    "id": int(message.id),
                    "time": message_time,
                    "text": str(message.message or ""),
                    "reply_to": int(getattr(message, "reply_to_msg_id", 0) or 0),
                }
            )

    signals: list[SignalRow] = []
    side_hint: str | None = None
    side_hint_time: datetime | None = None
    for row in sorted(raw_messages, key=lambda value: value["time"]):
        text = row["text"]
        if _is_phoenix_direction_runner_announcement(text):
            side_hint = _phoenix_direction_hint(text)
            side_hint_time = row["time"]
            continue
        fresh_hint = side_hint if side_hint_time and row["time"] - side_hint_time <= timedelta(minutes=20) else None
        signal = _parse_signal(
            text,
            f"management:{row['id']}",
            CHANNEL_ID,
            CHANNEL_TITLE,
            "",
            int(row["id"]),
            side_hint=fresh_hint,
        )
        if signal is None or signal.asset != "gold" or len(signal.entries) < 2 or not signal.tps:
            continue
        broker_time = row["time"] + broker_offset
        start_idx = int(rates["time"].searchsorted(pd.Timestamp(broker_time).ceil("min"), side="left"))
        if start_idx >= len(rates):
            continue
        market = float(rates.iloc[start_idx]["open"])
        signal = _repair_gold_hundred_digit_typo(signal, market)
        if not _phoenix_levels_plausible_against_market(signal, market):
            continue
        signals.append(SignalRow(int(row["id"]), row["time"], signal, start_idx, market))

    signal_times = sorted((item.time, int(item.message_id)) for item in signals)
    signal_ids = {message_id for _, message_id in signal_times}
    by_signal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    delays: dict[str, list[float]] = defaultdict(list)
    for message in raw_messages:
            message_time = message["time"]
            text = message["text"]
            kind, level = _classify(text)
            if not kind:
                continue
            reply_to = int(message["reply_to"] or 0)
            target_id = reply_to if reply_to in signal_ids else 0
            if not target_id:
                prior = [item for item in signal_times if timedelta(0) <= message_time - item[0] <= timedelta(hours=6)]
                if prior:
                    target_id = prior[-1][1]
            counts[kind] += 1
            if level:
                counts[f"tp{level}_hit"] += 1
            if len(examples[kind]) < 8:
                examples[kind].append(text[:240])
            if not target_id:
                counts[f"{kind}_unmatched"] += 1
                continue
            signal_time = next(value for value, message_id in signal_times if message_id == target_id)
            delay_seconds = (message_time - signal_time).total_seconds()
            delays[kind].append(delay_seconds)
            by_signal[target_id].append(
                {
                    "message_id": int(message["id"]),
                    "kind": kind,
                    "level": level,
                    "time": message_time.isoformat(),
                    "broker_time": pd.Timestamp(message_time + broker_offset).tz_convert("UTC"),
                    "delay_seconds": delay_seconds,
                    "text": text[:500],
                }
            )
    timing = {
        kind: {
            "matched": len(values),
            "median_minutes": round(float(np.median(values)) / 60.0, 2),
            "p25_minutes": round(float(np.percentile(values, 25)) / 60.0, 2),
            "p75_minutes": round(float(np.percentile(values, 75)) / 60.0, 2),
        }
        for kind, values in delays.items()
        if values
    }
    return signals, by_signal, {"counts": dict(counts), "timing": timing, "examples": dict(examples)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=90)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/phoenix_provider_management_90sessions.json")
    args = parser.parse_args()
    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(ROOT / env_file, override=True)
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(args.broker_offset_hours)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        rates = _rates(symbol, end - timedelta(days=max(220, args.sessions * 2)), end + timedelta(hours=1))
        sessions, cutoff = _completed_sessions(rates, end, args.sessions)
        rates = rates[(rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))].reset_index(drop=True)
        signals, updates, message_stats = await _channel_history(
            cfg,
            cutoff,
            timedelta(hours=args.broker_offset_hours),
            rates,
        )
        signals = sorted(signals, key=lambda item: item.time)
        signal_ids = [int(item.message_id) for item in signals]
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        profit_per_usd_001 = abs(float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0))
        candidate = Candidate("near", 30, 12.0, "none", False)
        baseline = _run_candidate(candidate, signals, rates, symbol, point, {}, args.commission_per_001, profit_per_usd_001)
        signal_by_id = {int(item.message_id): item for item in signals}
        variants: dict[str, Any] = {}
        for mode in ("pending_cancel", "explicit_be", "provider_ladder"):
            rows = []
            for row in baseline:
                signal_id = int(row["message_id"])
                management = updates.get(signal_id, [])
                if mode == "pending_cancel":
                    management = [item for item in management if item["kind"] in {"tp_hit", "secure_be", "close_cancel"}]
                replay = _managed_replay(
                    row,
                    management,
                    rates,
                    point,
                    profit_per_usd_001,
                    args.commission_per_001,
                    mode,
                    [float(value) for value in signal_by_id[signal_id].signal.tps],
                )
                rows.append({**row, **replay})
            values = [float(row["pnl_001"]) for row in rows]
            positive_deltas = sum(max(0.0, float(row["delta_001"])) for row in rows)
            negative_deltas = abs(sum(min(0.0, float(row["delta_001"])) for row in rows))
            variants[mode] = {
                "summary": _summary_values(values),
                "capital_protected_or_added_001": round(positive_deltas, 2),
                "profit_sacrificed_001": round(negative_deltas, 2),
                "net_change_vs_baseline_001": round(sum(float(row["delta_001"]) for row in rows), 2),
                "pending_cancelled_legs": sum(row["status"] == "pending_cancelled" for row in rows),
                "managed_stop_legs": sum(row["status"] == "managed_stop" for row in rows),
            }
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions": sessions,
            "signals": len(signal_ids),
            "method": "M1, dynamic spread, commission, conservative SL-first bars; provider messages applied no earlier than their Telegram timestamp.",
            "message_management": message_stats,
            "baseline": _summary(baseline, signal_ids),
            "variants": variants,
            "limitations": [
                "Updates without an explicit reply are assigned to the latest full signal within six hours.",
                "M1 candles cannot reproduce tick order or Telegram/broker latency inside one minute.",
                "The baseline uses six 0.01-lot legs at the near edge with a 30-minute pending window and a 12 USD SL cap.",
            ],
        }
        path = ROOT / args.output
        path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(json.dumps({key: output[key] for key in ("range_utc", "signals", "message_management", "baseline", "variants")}, indent=2, ensure_ascii=True))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
