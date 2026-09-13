from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _is_cancel_message,
    _is_secure_message,
    _phoenix_market_runner_allowed,
    _repair_tps_for_entry,
    _strict_live_tps_for_entry,
    _tp_hit_level,
)
from scripts.backtest_phoenix_complete_60d import CHANNEL_ID, SignalRow, _fetch_history, _rates


@dataclass(frozen=True)
class Candidate:
    entry_map: str
    pending_minutes: int
    provider_sl_cap: float
    protect_profile: str
    channel_cancel: bool


PROTECT_PROFILES: dict[str, tuple[str, ...]] = {
    "current": ("none", "be_tp3", "be_tp3", "be_tp1", "be_tp1", "ladder"),
    "fast_be": ("none", "be_tp1", "be_tp1", "be_tp1", "be_tp1", "ladder"),
    "delayed": ("none", "be_tp3", "be_tp3", "be_tp3", "be_tp3", "ladder"),
    "deep_tp2": ("none", "be_tp3", "be_tp3", "be_tp2", "be_tp2", "ladder"),
    "deep_ladder": ("none", "be_tp3", "be_tp3", "ladder", "ladder", "ladder"),
    "none": ("none", "none", "none", "none", "none", "none"),
    "extra_market": ("none", "be_tp1", "none", "none", "ladder", "none"),
}


def _entry_plan(side: str, entries: list[float], count: int, mode: str) -> list[float]:
    levels = sorted({round(float(value), 3) for value in entries if float(value or 0.0) > 0.0})
    if not levels:
        return []
    if len(levels) == 2:
        levels.insert(1, round((levels[0] + levels[1]) / 2.0, 3))
    if len(levels) > 3:
        levels = [levels[0], levels[len(levels) // 2], levels[-1]]
    near_to_far = list(reversed(levels)) if side == "buy" else list(levels)
    if mode == "legacy":
        planned = list(levels)
        return (planned + [planned[-1]] * count)[:count]
    if mode == "side_deep":
        planned = list(near_to_far)
        return (planned + [planned[-1]] * count)[:count]
    if mode == "cycle":
        return [near_to_far[index % len(near_to_far)] for index in range(count)]
    if mode == "near":
        return [near_to_far[0]] * count
    if mode == "middle":
        return [levels[len(levels) // 2]] * count
    raise ValueError(f"Unknown entry map: {mode}")


def _stop_with_cap(side: str, entry: float, provider_sl: float, cap: float, entries: list[float]) -> float:
    valid = (side == "buy" and 0.0 < provider_sl < entry) or (side == "sell" and provider_sl > entry)
    if not valid:
        edge = min(entries) if side == "buy" else max(entries)
        candidate = edge - 6.0 if side == "buy" else edge + 6.0
        candidate_valid = (
            side == "buy" and 0.0 < candidate < entry
        ) or (
            side == "sell" and candidate > entry
        )
        if not candidate_valid:
            fallback_distance = cap if cap > 0.0 else 6.0
            return entry - fallback_distance if side == "buy" else entry + fallback_distance
        if cap > 0.0 and abs(entry - candidate) > cap:
            return entry - cap if side == "buy" else entry + cap
        return candidate
    if cap <= 0.0 or abs(entry - provider_sl) <= cap:
        return provider_sl
    return entry - cap if side == "buy" else entry + cap


def _better_stop(side: str, current: float, candidate: float) -> float:
    if current <= 0.0:
        return candidate
    return max(current, candidate) if side == "buy" else min(current, candidate)


def _new_stop(side: str, entry: float, tps: list[float], reached: int, mode: str, current: float) -> float:
    trigger = {"be_tp1": 1, "be_tp2": 2, "be_tp3": 3}.get(mode, 99)
    if mode.startswith("be_tp") and reached >= trigger:
        candidate = entry + 0.10 if side == "buy" else entry - 0.10
        return _better_stop(side, current, candidate)
    if mode == "ladder" and reached >= 1:
        if reached == 1:
            candidate = entry + 0.10 if side == "buy" else entry - 0.10
        else:
            candidate = tps[min(reached - 2, len(tps) - 1)]
        return _better_stop(side, current, candidate)
    return current


def _simulate_leg(
    *,
    rates: pd.DataFrame,
    times: np.ndarray,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    spreads: np.ndarray,
    point: float,
    symbol: str,
    item: SignalRow,
    entry: float,
    target_index: int,
    protect_mode: str,
    pending_minutes: int,
    provider_sl_cap: float,
    cancel_time: pd.Timestamp | None,
    commission_per_001: float,
    profit_per_usd_001: float,
    market_runner_override: bool | None = None,
) -> dict[str, Any] | None:
    side = str(item.signal.side)
    tps = _repair_tps_for_entry(
        side,
        float(entry),
        [float(value) for value in item.signal.tps if float(value or 0.0) > 0.0],
        max(int(target_index), 4),
    )
    if target_index > len(tps):
        return None
    target = tps[target_index - 1]
    start_idx = int(item.start_idx)
    if start_idx >= len(rates):
        return None
    zone = sorted(float(value) for value in item.signal.entries if float(value or 0.0) > 0.0)
    if not zone:
        return None
    sl = _stop_with_cap(side, entry, float(item.signal.sl or 0.0), provider_sl_cap, zone)
    expiry_minutes = 120 if "LIMIT" in str(item.signal.raw_text or "").upper() else pending_minutes
    expiry_ns = times[start_idx] + np.timedelta64(int(expiry_minutes), "m")
    expiry_idx = min(int(np.searchsorted(times, expiry_ns, side="right")), len(rates))
    first_spread = float(spreads[start_idx]) * point
    market_bid = float(opens[start_idx])
    market_ask = market_bid + first_spread
    market_price = market_ask if side == "buy" else market_bid
    before_tp1 = market_bid < tps[0] if side == "buy" else market_ask > tps[0]
    trigger_idx = -1
    fill_price = float(entry)
    fill_kind = "limit"
    market_runner = bool(
        target_index == 1
        and before_tp1
        and _phoenix_market_runner_allowed(side, zone, market_price, tps)
    )
    if market_runner_override is not None:
        market_runner = bool(market_runner_override)
    if market_runner:
        trigger_idx = start_idx
        fill_price = market_price
        fill_kind = "market"
    else:
        if side == "buy":
            fill_kind = "limit" if entry < market_ask else "stop"
        else:
            fill_kind = "limit" if entry > market_bid else "stop"
        cancel_ns = np.datetime64(cancel_time.to_datetime64()) if cancel_time is not None else None
        for index in range(start_idx, expiry_idx):
            if cancel_ns is not None and times[index] >= cancel_ns:
                return None
            spread_price = float(spreads[index]) * point
            if side == "buy" and fill_kind == "limit":
                touched = float(lows[index]) + spread_price <= entry
            elif side == "sell" and fill_kind == "limit":
                touched = float(highs[index]) >= entry
            elif side == "buy":
                touched = float(highs[index]) + spread_price >= entry
            else:
                touched = float(lows[index]) <= entry
            if touched:
                trigger_idx = index
                break
    if trigger_idx < 0:
        return None

    # A market fill can change both sides of the risk/reward plan. Rebuild the
    # ladder and stop against the actual fill exactly as the live path does.
    tps = _repair_tps_for_entry(
        side,
        float(fill_price),
        [float(value) for value in item.signal.tps if float(value or 0.0) > 0.0],
        max(int(target_index), 4),
    )
    if target_index > len(tps):
        return None
    target = tps[target_index - 1]
    sl = _stop_with_cap(side, fill_price, float(item.signal.sl or 0.0), provider_sl_cap, zone)

    horizon_ns = times[trigger_idx] + np.timedelta64(6, "h")
    end_idx = min(int(np.searchsorted(times, horizon_ns, side="right")), len(rates))
    current_sl = float(sl)
    reached_level = 0
    exit_idx = max(trigger_idx, end_idx - 1)
    exit_price = float(closes[exit_idx])
    status = "timeout"
    for index in range(trigger_idx, end_idx):
        spread_price = float(spreads[index]) * point
        high = float(highs[index])
        low = float(lows[index])
        exit_high = high if side == "buy" else high + spread_price
        exit_low = low if side == "buy" else low + spread_price
        hit_sl = exit_low <= current_sl if side == "buy" else exit_high >= current_sl
        hit_tp = exit_high >= target if side == "buy" else exit_low <= target
        if hit_sl:
            status, exit_idx, exit_price = "stop", index, current_sl
            break
        if hit_tp:
            status, exit_idx, exit_price = "target", index, target
            break
        for level, tp in enumerate(tps, start=1):
            reached = exit_high >= tp if side == "buy" else exit_low <= tp
            if reached:
                reached_level = max(reached_level, level)
        current_sl = _new_stop(side, fill_price, tps, reached_level, protect_mode, current_sl)

    signed_move = exit_price - fill_price if side == "buy" else fill_price - exit_price
    pnl = signed_move * float(profit_per_usd_001) - float(commission_per_001)
    return {
        "message_id": int(item.message_id),
        "signal_time": item.time.isoformat(),
        "entry_time": pd.Timestamp(times[trigger_idx]).isoformat(),
        "exit_time": pd.Timestamp(times[exit_idx]).isoformat(),
        "side": side,
        "target_index": int(target_index),
        "protect_mode": protect_mode,
        "entry": round(fill_price, 3),
        "sl": round(sl, 3),
        "tp": round(target, 3),
        "fill_kind": fill_kind,
        "status": status,
        "pnl_001": round(pnl, 4),
    }


def _summary(trades: list[dict[str, Any]], signal_ids: list[int]) -> dict[str, Any]:
    selected = [row for row in trades if int(row["message_id"]) in set(signal_ids)]
    by_signal: dict[int, float] = defaultdict(float)
    for row in selected:
        by_signal[int(row["message_id"])] += float(row["pnl_001"])
    pnl = round(sum(float(row["pnl_001"]) for row in selected), 2)
    wins = sum(1 for row in selected if float(row["pnl_001"]) > 0.0)
    losses = sum(1 for row in selected if float(row["pnl_001"]) < 0.0)
    gross_win = sum(max(0.0, float(row["pnl_001"])) for row in selected)
    gross_loss = abs(sum(min(0.0, float(row["pnl_001"])) for row in selected))
    balance = peak = 0.0
    max_dd = 0.0
    for row in sorted(selected, key=lambda value: value["exit_time"]):
        balance += float(row["pnl_001"])
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
    positive_signals = sum(1 for value in by_signal.values() if value > 0.0)
    negative_signals = sum(1 for value in by_signal.values() if value < 0.0)
    return {
        "signals": len(signal_ids),
        "executed_signals": len(by_signal),
        "legs": len(selected),
        "wins": wins,
        "losses": losses,
        "leg_win_rate_pct": round(wins / max(1, wins + losses) * 100.0, 2),
        "positive_signals": positive_signals,
        "negative_signals": negative_signals,
        "signal_win_rate_pct": round(positive_signals / max(1, positive_signals + negative_signals) * 100.0, 2),
        "pnl_001": pnl,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_001": round(max_dd, 2),
    }


async def _channel_cancel_times(
    cfg,
    cutoff: datetime,
    broker_offset: timedelta,
) -> dict[int, pd.Timestamp]:
    session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
    client = TelegramClient(str(session.resolve()), int(cfg.telegram_api_id), cfg.telegram_api_hash)
    result: dict[int, pd.Timestamp] = {}
    async with client:
        async for message in client.iter_messages(CHANNEL_ID, offset_date=datetime.now(UTC), reverse=False):
            message_time = message.date.astimezone(UTC)
            if message_time < cutoff:
                break
            reply_to = int(getattr(message, "reply_to_msg_id", 0) or 0)
            if reply_to <= 0:
                continue
            text = str(message.message or "")
            should_cancel = _tp_hit_level(text) >= 2 or _is_secure_message(text) or _is_cancel_message(text)
            if should_cancel:
                timestamp = pd.Timestamp(message_time + broker_offset)
                if reply_to not in result or timestamp < result[reply_to]:
                    result[reply_to] = timestamp
    return result


def _run_candidate(
    candidate: Candidate,
    signals: list[SignalRow],
    rates: pd.DataFrame,
    symbol: str,
    point: float,
    cancel_times: dict[int, pd.Timestamp],
    commission_per_001: float,
    profit_per_usd_001: float,
    force_market: bool = False,
) -> list[dict[str, Any]]:
    times = rates["time"].to_numpy(dtype="datetime64[ns]")
    opens = rates["open"].to_numpy(dtype=float)
    highs = rates["high"].to_numpy(dtype=float)
    lows = rates["low"].to_numpy(dtype=float)
    closes = rates["close"].to_numpy(dtype=float)
    spreads = rates["spread"].to_numpy(dtype=float)
    protect_modes = PROTECT_PROFILES[candidate.protect_profile]
    trades: list[dict[str, Any]] = []
    for item in signals:
        # Live Phoenix repairs a short TP ladder before placing the configured
        # TP1/TP5/TP6 plan, so replay the same six-slot target plan here.
        target_count = 6
        if target_count <= 0 or len(item.signal.entries) < 2:
            continue
        entries = _entry_plan(item.signal.side, item.signal.entries, target_count, candidate.entry_map)
        cancel_time = cancel_times.get(int(item.message_id)) if candidate.channel_cancel else None
        for target_index, entry in enumerate(entries, start=1):
            if force_market:
                live_tps = _strict_live_tps_for_entry(item.signal.side, float(item.market), item.signal.tps)
                if len(live_tps) < target_index:
                    continue
            trade = _simulate_leg(
                rates=rates,
                times=times,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                spreads=spreads,
                point=point,
                symbol=symbol,
                item=item,
                entry=float(entry),
                target_index=target_index,
                protect_mode=protect_modes[target_index - 1],
                pending_minutes=candidate.pending_minutes,
                provider_sl_cap=candidate.provider_sl_cap,
                cancel_time=cancel_time,
                commission_per_001=commission_per_001,
                profit_per_usd_001=profit_per_usd_001,
                market_runner_override=True if force_market else None,
            )
            if trade is not None:
                trades.append(trade)
    return trades


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trading-days", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/optimize_phoenix_copy_execution_60sessions.json")
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument("--replay-entry-map", default="near", choices=("legacy", "side_deep", "cycle", "near", "middle"))
    parser.add_argument("--replay-pending-minutes", type=int, default=15)
    parser.add_argument("--replay-provider-sl-cap", type=float, default=12.0)
    parser.add_argument("--replay-protect-profile", default="deep_ladder", choices=tuple(PROTECT_PROFILES))
    parser.add_argument("--replay-channel-cancel", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--replay-target-indices", default="1,5,6")
    parser.add_argument("--replay-force-market", action="store_true")
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    broker_offset = timedelta(hours=float(args.broker_offset_hours))
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(float(args.broker_offset_hours))
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        probe_start = end - timedelta(days=max(100, int(args.trading_days * 1.8)))
        rates = _rates(symbol, probe_start, end + timedelta(hours=1))
        sessions = sorted({timestamp.date() for timestamp in rates["time"] if int(timestamp.weekday()) < 5})
        if len(sessions) < int(args.trading_days):
            raise RuntimeError(f"Only {len(sessions)} sessions available")
        cutoff = datetime.combine(sessions[-int(args.trading_days)], datetime.min.time(), tzinfo=UTC)
        rates = rates[rates["time"] >= pd.Timestamp(cutoff)].reset_index(drop=True)
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, _ = await _fetch_history(rates, cutoff, session)
        signals = sorted(
            [item for item in signals if len(item.signal.entries) >= 2 and len(item.signal.tps) >= 1],
            key=lambda item: item.time,
        )
        cancel_times = {} if args.replay_only and not args.replay_channel_cancel else await _channel_cancel_times(cfg, cutoff, broker_offset)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate XAUUSD 0.01-lot value per USD")
        signal_ids = [int(item.message_id) for item in signals]
        split = max(1, int(len(signal_ids) * 0.60))
        train_ids, holdout_ids = signal_ids[:split], signal_ids[split:]

        if args.replay_only:
            target_indices = {
                int(value.strip())
                for value in str(args.replay_target_indices).split(",")
                if value.strip()
            }
            candidate = Candidate(
                str(args.replay_entry_map),
                max(1, int(args.replay_pending_minutes)),
                max(0.0, float(args.replay_provider_sl_cap)),
                str(args.replay_protect_profile),
                bool(args.replay_channel_cancel),
            )
            candidate_trades = [
                trade
                for trade in _run_candidate(
                    candidate,
                    signals,
                    rates,
                    symbol,
                    point,
                    cancel_times,
                    float(args.commission_per_001),
                    profit_per_usd_001,
                    bool(args.replay_force_market),
                )
                if int(trade["target_index"]) in target_indices
            ]
            output = {
                "generated_utc": datetime.now(UTC).isoformat(),
                "range": {"start": cutoff.isoformat(), "end": end.isoformat(), "trading_days": int(args.trading_days)},
                "method": "single Phoenix candidate replay on Vantage M1 bid candles; dynamic spread; SL-first ambiguous bars; commission included",
                "broker_offset_hours": float(args.broker_offset_hours),
                "symbol": symbol,
                "signals": len(signals),
                "train_signals": len(train_ids),
                "holdout_signals": len(holdout_ids),
                "commission_per_001": float(args.commission_per_001),
                "candidate": candidate.__dict__,
                "target_indices": sorted(target_indices),
                "force_market": bool(args.replay_force_market),
                "train": _summary(candidate_trades, train_ids),
                "holdout": _summary(candidate_trades, holdout_ids),
                "full": _summary(candidate_trades, signal_ids),
                "trades": candidate_trades,
            }
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
            print(json.dumps({key: output[key] for key in ("range", "signals", "candidate", "target_indices", "train", "holdout", "full")}, indent=2))
            return

        stage_one: list[dict[str, Any]] = []
        for entry_map in ("legacy", "side_deep", "cycle", "near", "middle"):
            for pending_minutes in (15, 30, 60, 120):
                for provider_sl_cap in (0.0, 8.0, 12.0):
                    for channel_cancel in (True, False):
                        candidate = Candidate(entry_map, pending_minutes, provider_sl_cap, "none", channel_cancel)
                        trades = _run_candidate(
                            candidate,
                            signals,
                            rates,
                            symbol,
                            point,
                            cancel_times,
                            float(args.commission_per_001),
                            profit_per_usd_001,
                        )
                        stage_one.append(
                            {
                                "candidate": candidate.__dict__,
                                "train": _summary(trades, train_ids),
                                "holdout": _summary(trades, holdout_ids),
                                "full": _summary(trades, signal_ids),
                            }
                        )
        robust_stage_one = [
            row for row in stage_one
            if row["train"]["pnl_001"] > 0 and row["holdout"]["pnl_001"] > 0
        ]
        robust_stage_one.sort(
            key=lambda row: (
                min(row["train"]["pnl_001"], row["holdout"]["pnl_001"]),
                row["holdout"]["signal_win_rate_pct"],
                row["full"]["pnl_001"],
            ),
            reverse=True,
        )
        bases = robust_stage_one[:12] if robust_stage_one else sorted(
            stage_one, key=lambda row: row["full"]["pnl_001"], reverse=True
        )[:12]

        final_rows: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for base in bases:
            base_candidate = base["candidate"]
            for profile in PROTECT_PROFILES:
                candidate = Candidate(
                    str(base_candidate["entry_map"]),
                    int(base_candidate["pending_minutes"]),
                    float(base_candidate["provider_sl_cap"]),
                    profile,
                    bool(base_candidate["channel_cancel"]),
                )
                key = tuple(candidate.__dict__.values())
                if key in seen:
                    continue
                seen.add(key)
                trades = _run_candidate(
                    candidate,
                    signals,
                    rates,
                    symbol,
                    point,
                    cancel_times,
                    float(args.commission_per_001),
                    profit_per_usd_001,
                )
                final_rows.append(
                    {
                        "candidate": candidate.__dict__,
                        "train": _summary(trades, train_ids),
                        "holdout": _summary(trades, holdout_ids),
                        "full": _summary(trades, signal_ids),
                    }
                )
        final_rows.sort(
            key=lambda row: (
                row["train"]["pnl_001"] > 0 and row["holdout"]["pnl_001"] > 0,
                min(row["train"]["pnl_001"], row["holdout"]["pnl_001"]),
                row["holdout"]["signal_win_rate_pct"],
                row["full"]["pnl_001"],
            ),
            reverse=True,
        )

        current_candidate = Candidate("legacy", 60, 0.0, "current", True)
        current_trades = _run_candidate(
            current_candidate,
            signals,
            rates,
            symbol,
            point,
            cancel_times,
            float(args.commission_per_001),
            profit_per_usd_001,
        )
        selected = final_rows[0] if final_rows else None
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": cutoff.isoformat(), "end": end.isoformat(), "trading_days": int(args.trading_days)},
            "method": "chronological Phoenix full-signal replay on Vantage M1 bid candles; Telegram UTC shifted to broker candle clock; per-bar spread reconstructs ask; SL-first ambiguous bars; Telegram reply_to scopes TP2/secure cancellation; commission included",
            "broker_offset_hours": float(args.broker_offset_hours),
            "symbol": symbol,
            "signals": len(signals),
            "train_signals": len(train_ids),
            "holdout_signals": len(holdout_ids),
            "channel_cancel_updates": len(cancel_times),
            "commission_per_001": float(args.commission_per_001),
            "current": {
                "candidate": current_candidate.__dict__,
                "train": _summary(current_trades, train_ids),
                "holdout": _summary(current_trades, holdout_ids),
                "full": _summary(current_trades, signal_ids),
            },
            "selected": selected,
            "top20": final_rows[:20],
            "stage_one_top10": robust_stage_one[:10],
        }
        if selected is not None:
            selected_candidate = Candidate(**selected["candidate"])
            output["selected_trades"] = _run_candidate(
                selected_candidate,
                signals,
                rates,
                symbol,
                point,
                cancel_times,
                float(args.commission_per_001),
                profit_per_usd_001,
            )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({key: output[key] for key in ("range", "signals", "current", "selected")}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
