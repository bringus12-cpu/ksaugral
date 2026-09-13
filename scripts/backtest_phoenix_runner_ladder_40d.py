from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _parse_signal,
    _phoenix_levels_plausible_against_market,
    _phoenix_market_runner_allowed,
    _phoenix_progressive_stop,
    _repair_gold_hundred_digit_typo,
    _repair_tps_for_entry,
    _select_live_tps,
    _strict_live_tps_for_entry,
)
from scripts.backtest_phoenix_complete_60d import _rates as _shared_rates


CHANNEL_ID = -1002864291293
CHANNEL_TITLE = "PHOENIX VIP"
LOT = 0.01
PENDING_MINUTES = 15.0
HORIZON_HOURS = 6.0


@dataclass
class SignalRow:
    dt: datetime
    message_id: int
    signal: Any
    start_idx: int
    market: float


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    return enrich(_shared_rates(symbol, start, end))


def _profit(symbol: str, side: str, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return round(float(mt5.order_calc_profit(order_type, symbol, LOT, entry, exit_price) or 0.0), 2)


def _hit_tp(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _hit_sl(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _entry_touched(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _pending_valid(side: str, entry: float, market: float) -> bool:
    return entry < market if side == "buy" else entry > market


def _simulate_leg(
    symbol: str,
    signal: Any,
    rates: pd.DataFrame,
    start_idx: int,
    entry: float,
    is_pending: bool,
    target_index: int,
    protect: str,
    sl_cap: float = 6.0,
) -> dict[str, Any]:
    try:
        repaired_tps = _repair_tps_for_entry(signal.side, entry, signal.tps, max(target_index, 4))
        tp1, target, live_tp_index, live_tps = _select_live_tps(signal.side, entry, repaired_tps, target_index)
    except Exception as exc:
        return {"status": "skip", "reason": f"no_live_tp:{exc}", "target": target_index}

    sl = float(signal.sl or 0.0)
    if signal.side == "buy" and sl >= entry:
        sl = entry - 4.0
    if signal.side == "sell" and sl <= entry:
        sl = entry + 4.0
    if sl_cap > 0 and abs(entry - sl) > sl_cap:
        sl = entry - sl_cap if signal.side == "buy" else entry + sl_cap

    trigger_idx = start_idx
    if is_pending:
        market = float(rates.iloc[start_idx]["close"])
        if not _pending_valid(signal.side, entry, market):
            return {"status": "skip", "reason": "wrong_pending_side", "target": live_tp_index}
        expiry = rates.iloc[start_idx]["time"] + pd.Timedelta(minutes=PENDING_MINUTES)
        expiry_idx = min(int(rates["time"].searchsorted(expiry, side="right")), len(rates))
        trigger_idx = -1
        for idx in range(start_idx, expiry_idx):
            row = rates.iloc[idx]
            high, low = float(row["high"]), float(row["low"])
            if _hit_tp(signal.side, tp1, high, low):
                return {"status": "skip", "reason": "tp_before_entry", "target": live_tp_index}
            if _entry_touched(entry, high, low):
                trigger_idx = idx
                break
        if trigger_idx < 0:
            return {"status": "skip", "reason": "not_triggered", "target": live_tp_index}

    end_time = rates.iloc[trigger_idx]["time"] + pd.Timedelta(hours=HORIZON_HOURS)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    entry_time = rates.iloc[trigger_idx]["time"].isoformat()
    current_sl = sl
    reached_level = 0
    for idx in range(trigger_idx, end_idx):
        row = rates.iloc[idx]
        high, low = float(row["high"]), float(row["low"])
        if _hit_sl(signal.side, current_sl, high, low):
            pnl = _profit(symbol, signal.side, entry, current_sl)
            if pnl > 0.05:
                status = "protected_win"
            elif pnl < -0.05:
                status = "loss"
            else:
                status = "be"
            return {
                "status": status,
                "target": live_tp_index,
                "entry": round(entry, 2),
                "initial_sl": round(sl, 2),
                "exit": round(current_sl, 2),
                "entry_time": entry_time,
                "exit_time": rates.iloc[idx]["time"].isoformat(),
                "pnl": pnl,
            }
        if _hit_tp(signal.side, target, high, low):
            return {
                "status": "win",
                "target": live_tp_index,
                "entry": round(entry, 2),
                "initial_sl": round(sl, 2),
                "exit": round(target, 2),
                "entry_time": entry_time,
                "exit_time": rates.iloc[idx]["time"].isoformat(),
                "pnl": _profit(symbol, signal.side, entry, target),
            }
        previous_reached = reached_level
        for level, tp in enumerate(live_tps, start=1):
            if level > reached_level and _hit_tp(signal.side, float(tp), high, low):
                reached_level = level
        if protect in {"phoenix_ladder", "phoenix_delayed_ladder"} and reached_level > previous_reached:
            if protect == "phoenix_delayed_ladder" and reached_level == 2:
                new_sl = None
            elif protect == "phoenix_delayed_ladder" and reached_level >= 3:
                delayed_level = max(1, reached_level - 1)
                new_sl = _phoenix_progressive_stop(signal.side, entry, live_tps, delayed_level, current_sl)
            else:
                new_sl = _phoenix_progressive_stop(signal.side, entry, live_tps, reached_level, current_sl)
            if new_sl is not None:
                current_sl = new_sl
        elif protect == "be" and _hit_tp(signal.side, tp1, high, low):
            current_sl = max(current_sl, entry) if signal.side == "buy" else min(current_sl, entry)
        elif protect.startswith("be_plus_") and _hit_tp(signal.side, tp1, high, low):
            buffer = float(protect.removeprefix("be_plus_").replace("p", "."))
            protected_sl = entry + buffer if signal.side == "buy" else entry - buffer
            current_sl = max(current_sl, protected_sl) if signal.side == "buy" else min(current_sl, protected_sl)
        elif protect == "be_after_tp2" and reached_level >= 2:
            current_sl = max(current_sl, entry) if signal.side == "buy" else min(current_sl, entry)
        elif protect == "be_after_tp3" and reached_level >= 3:
            current_sl = max(current_sl, entry) if signal.side == "buy" else min(current_sl, entry)
        elif protect == "tp1_after_tp3" and reached_level >= 3:
            current_sl = max(current_sl, tp1) if signal.side == "buy" else min(current_sl, tp1)

    exit_price = float(rates.iloc[max(trigger_idx, end_idx - 1)]["close"])
    return {
        "status": "timeout",
        "target": live_tp_index,
        "entry": round(entry, 2),
        "initial_sl": round(sl, 2),
        "exit": round(exit_price, 2),
        "entry_time": entry_time,
        "exit_time": rates.iloc[max(trigger_idx, end_idx - 1)]["time"].isoformat(),
        "pnl": _profit(symbol, signal.side, entry, exit_price),
    }


def _plans(
    signal: Any,
    market: float,
    extra_trigger_runners: int = 0,
    allow_market_runners: bool = True,
    unique_entry_runners: bool = False,
    deep_target_analysis: bool = False,
) -> list[tuple[str, float, bool, int, str]]:
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    if not entries:
        entries = [float(signal.entry or market)]
    plans: list[tuple[str, float, bool, int, str]] = []
    if allow_market_runners and _phoenix_market_runner_allowed(signal.side, entries, market, signal.tps):
        plans.append(("runner_tp1", market, False, 1, "be"))
        plans.append(("runner_tp6", market, False, 6, "phoenix_ladder"))
    target_plan = [(1, "none"), (1, "be"), (2, "be"), (4, "be"), (6, "phoenix_ladder")]
    entries_to_place = list(entries[: len(target_plan)])
    if len(entries_to_place) < len(target_plan):
        entries_to_place.extend([entries_to_place[-1]] * (len(target_plan) - len(entries_to_place)))
    seen_extra_entries: set[float] = set()
    for idx, (entry, (target, protect)) in enumerate(zip(entries_to_place, target_plan), start=1):
        plans.append((f"pending_e{idx}_tp{target}", float(entry), True, target, protect))
        entry_key = round(float(entry), 2)
        if unique_entry_runners and entry_key in seen_extra_entries:
            continue
        seen_extra_entries.add(entry_key)
        for runner_no in range(1, max(0, int(extra_trigger_runners)) + 1):
            runner_target = (4, 6)[runner_no - 1] if runner_no <= 2 else min(max(target + runner_no + 1, 3), 10)
            plans.append((f"pending_e{idx}_runner{runner_no}_tp{runner_target}", float(entry), True, runner_target, "phoenix_ladder"))
    if deep_target_analysis:
        market_entry_allowed = allow_market_runners and _phoenix_market_runner_allowed(signal.side, entries, market, signal.tps)
        if market_entry_allowed:
            adaptive_entry = float(market)
            adaptive_pending = False
        else:
            valid_entries = [entry for entry in entries if _pending_valid(signal.side, float(entry), market)]
            adaptive_entry = min(valid_entries, key=lambda value: abs(float(value) - market)) if valid_entries else 0.0
            adaptive_pending = True
        live_targets = _strict_live_tps_for_entry(signal.side, adaptive_entry, signal.tps) if adaptive_entry > 0 else []
        entry_mode = "pending" if adaptive_pending else "market"
        for target in range(1, min(8, len(live_targets)) + 1):
            plans.append(
                (
                    f"reach_{entry_mode}_tp{target}_none",
                    float(adaptive_entry),
                    adaptive_pending,
                    target,
                    "none",
                )
            )
        protect_modes = (
            "none",
            "be",
            "be_plus_0p25",
            "be_plus_0p50",
            "be_after_tp2",
            "be_after_tp3",
            "tp1_after_tp3",
            "phoenix_ladder",
            "phoenix_delayed_ladder",
        )
        for target in (5, 6, 7, 8):
            if len(live_targets) < target:
                continue
            for protect in protect_modes:
                plans.append(
                    (
                        f"deep_{entry_mode}_tp{target}_{protect}",
                        float(adaptive_entry),
                        adaptive_pending,
                        target,
                        protect,
                    )
                )
        deepest_target = min(8, len(live_targets))
        if deepest_target >= 5:
            for protect in protect_modes:
                plans.append(
                    (
                        f"deepest_{entry_mode}_tp{deepest_target}_{protect}",
                        float(adaptive_entry),
                        adaptive_pending,
                        deepest_target,
                        protect,
                    )
                )
    return plans


async def _fetch_signals(rates: pd.DataFrame, days: int, broker_time_offset_minutes: int = 0) -> list[SignalRow]:
    cfg = load_settings()
    session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
    client = TelegramClient(str(session), int(cfg.telegram_api_id), cfg.telegram_api_hash)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[SignalRow] = []
    async with client:
        async for msg in client.iter_messages(CHANNEL_ID, offset_date=datetime.now(UTC), reverse=False):
            dt = msg.date.astimezone(UTC)
            if dt < cutoff:
                break
            text = str(msg.message or "")
            signal = _parse_signal(text, f"{CHANNEL_ID}:{msg.id}", CHANNEL_ID, CHANNEL_TITLE, "", int(msg.id))
            if signal is None or signal.asset != "gold":
                continue
            candle_time = pd.Timestamp(dt) + pd.Timedelta(minutes=int(broker_time_offset_minutes))
            idx = int(rates["time"].searchsorted(candle_time, side="left"))
            if idx >= len(rates):
                continue
            market = float(rates.iloc[idx]["close"])
            repaired = _repair_gold_hundred_digit_typo(signal, market)
            if not _phoenix_levels_plausible_against_market(repaired, market):
                continue
            out.append(SignalRow(dt=dt, message_id=int(msg.id), signal=repaired, start_idx=idx, market=market))
    return list(reversed(out))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=40)
    parser.add_argument("--extra-trigger-runners", type=int, default=0)
    parser.add_argument("--unique-entry-runners", action="store_true")
    parser.add_argument("--no-market-runners", action="store_true")
    parser.add_argument("--deep-target-analysis", action="store_true")
    parser.add_argument("--broker-time-offset-minutes", type=int, default=0)
    parser.add_argument("--sl-cap", type=float, default=6.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_settings()
    creds = Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path)
    connect(creds)
    try:
        symbol = ensure_symbol(cfg.symbol)
        broker_offset = timedelta(minutes=int(args.broker_time_offset_minutes))
        end = datetime.now(UTC) + broker_offset
        start = end - timedelta(days=max(2, int(args.days)) + 2)
        rates = _rates(symbol, start, end)
        symbol_info = mt5.symbol_info(symbol)
        point = float(getattr(symbol_info, "point", 0.01) or 0.01)
        median_spread_usd = float(rates["spread"].median()) * point if "spread" in rates.columns else 0.0
        spread_anchor = float(rates.iloc[-1]["close"])
        spread_cost_per_leg = (
            abs(_profit(symbol, "buy", spread_anchor, spread_anchor + median_spread_usd))
            if median_spread_usd > 0
            else 0.0
        )
        signals = await _fetch_signals(rates, int(args.days), int(args.broker_time_offset_minutes))
        legs: list[dict[str, Any]] = []
        skips: dict[str, int] = {}
        for item in signals:
            for name, entry, pending, target, protect in _plans(
                item.signal,
                item.market,
                int(args.extra_trigger_runners),
                not bool(args.no_market_runners),
                bool(args.unique_entry_runners),
                bool(args.deep_target_analysis),
            ):
                result = _simulate_leg(
                    symbol,
                    item.signal,
                    rates,
                    item.start_idx,
                    entry,
                    pending,
                    target,
                    protect,
                    sl_cap=max(0.0, float(args.sl_cap)),
                )
                if result.get("status") == "skip":
                    skips[str(result.get("reason", "skip"))] = skips.get(str(result.get("reason", "skip")), 0) + 1
                    continue
                legs.append(
                    {
                        "time": item.dt.isoformat(),
                        "message_id": item.message_id,
                        "leg": name,
                        "side": item.signal.side,
                        **result,
                    }
                )

        wins = sum(1 for leg in legs if leg["status"] in {"win", "protected_win"})
        losses = sum(1 for leg in legs if leg["status"] == "loss")
        be = sum(1 for leg in legs if leg["status"] == "be")
        timeouts = sum(1 for leg in legs if leg["status"] == "timeout")
        pnl = round(sum(float(leg.get("pnl", 0.0) or 0.0) for leg in legs), 2)
        by_leg: dict[str, dict[str, Any]] = {}
        for leg in legs:
            bucket = by_leg.setdefault(
                leg["leg"],
                {
                    "legs": 0,
                    "targets_hit": 0,
                    "protected_wins": 0,
                    "wins": 0,
                    "losses": 0,
                    "be": 0,
                    "timeouts": 0,
                    "pnl": 0.0,
                    "pnl_after_spread": 0.0,
                },
            )
            bucket["legs"] += 1
            bucket["pnl"] = round(float(bucket["pnl"]) + float(leg.get("pnl", 0.0) or 0.0), 2)
            bucket["pnl_after_spread"] = round(
                float(bucket["pnl_after_spread"]) + float(leg.get("pnl", 0.0) or 0.0) - spread_cost_per_leg,
                2,
            )
            if leg["status"] in {"win", "protected_win"}:
                bucket["wins"] += 1
                if leg["status"] == "win":
                    bucket["targets_hit"] += 1
                else:
                    bucket["protected_wins"] += 1
            elif leg["status"] == "loss":
                bucket["losses"] += 1
            elif leg["status"] == "be":
                bucket["be"] += 1
            else:
                bucket["timeouts"] += 1
        for bucket in by_leg.values():
            closed = int(bucket["wins"]) + int(bucket["losses"])
            bucket["winrate"] = round(100.0 * int(bucket["wins"]) / closed, 2) if closed else 0.0
            bucket["target_hit_rate"] = round(100.0 * int(bucket["targets_hit"]) / max(1, int(bucket["legs"])), 2)

        ordered_times = sorted({str(leg["time"]) for leg in legs})
        split_time = ordered_times[max(0, min(len(ordered_times) - 1, int(len(ordered_times) * 0.60)))] if ordered_times else ""
        for leg_name, bucket in by_leg.items():
            for split_name, predicate in (
                ("train", lambda value: value < split_time),
                ("holdout", lambda value: value >= split_time),
            ):
                subset = [leg for leg in legs if leg["leg"] == leg_name and predicate(str(leg["time"]))]
                split_wins = sum(1 for leg in subset if leg["status"] in {"win", "protected_win"})
                split_losses = sum(1 for leg in subset if leg["status"] == "loss")
                split_be = sum(1 for leg in subset if leg["status"] == "be")
                gross_profit = sum(max(0.0, float(leg.get("pnl", 0.0) or 0.0) - spread_cost_per_leg) for leg in subset)
                gross_loss = abs(sum(min(0.0, float(leg.get("pnl", 0.0) or 0.0) - spread_cost_per_leg) for leg in subset))
                bucket[split_name] = {
                    "legs": len(subset),
                    "wins": split_wins,
                    "losses": split_losses,
                    "be": split_be,
                    "winrate": round(100.0 * split_wins / max(1, split_wins + split_losses), 2),
                    "pnl_after_spread": round(sum(float(leg.get("pnl", 0.0) or 0.0) - spread_cost_per_leg for leg in subset), 2),
                    "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
                }

        summary = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "channel": CHANNEL_TITLE,
            "days": int(args.days),
            "extra_trigger_runners_per_pending": int(args.extra_trigger_runners),
            "unique_entry_runners": bool(args.unique_entry_runners),
            "market_runners_enabled": not bool(args.no_market_runners),
            "lot_per_leg": LOT,
            "pending_minutes": PENDING_MINUTES,
            "horizon_hours": HORIZON_HOURS,
            "deep_target_analysis": bool(args.deep_target_analysis),
            "broker_time_offset_minutes": int(args.broker_time_offset_minutes),
            "sl_cap_usd": max(0.0, float(args.sl_cap)),
            "median_spread_usd": round(median_spread_usd, 4),
            "spread_cost_per_leg": round(spread_cost_per_leg, 4),
            "signals": len(signals),
            "legs": len(legs),
            "wins": wins,
            "losses": losses,
            "be": be,
            "timeouts": timeouts,
            "winrate_closed": round(100.0 * wins / max(1, wins + losses), 2),
            "pnl": pnl,
            "by_leg": by_leg,
            "skips": skips,
            "selected_extra_tp6_runner_events": [
                leg for leg in legs if leg.get("leg") == "deep_market_tp6_be_after_tp2"
            ],
            "sample": legs[-30:],
        }
        out = cfg.data_dir / (args.output or f"phoenix_runner_ladder_{int(args.days)}d_extra{int(args.extra_trigger_runners)}_lot001.json")
        out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
