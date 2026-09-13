from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _is_phoenix_direction_runner_announcement,
    _phoenix_direction_hint,
)
from scripts.backtest_phoenix_complete_60d import _rates


CHANNEL_ID = -1002864291293
RANGE_RE = re.compile(r"^\s*(\d{3,5}(?:[.,]\d+)?)\s*[/\-]\s*(\d{3,5}(?:[.,]\d+)?)\s*$")
SL_DISTANCES = (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0)


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _simulate(
    candidate: dict[str, Any],
    rates: pd.DataFrame,
    symbol: str,
    spread_cost_1lot: float,
    tp_distance: float,
    sl_distance: float,
    be_trigger: float,
    be_buffer: float,
) -> dict[str, Any]:
    idx = int(candidate["start_idx"])
    side = str(candidate["side"])
    entry = float(candidate["entry"])
    tp = entry + tp_distance if side == "buy" else entry - tp_distance
    initial_sl = entry - sl_distance if side == "buy" else entry + sl_distance
    current_sl = initial_sl
    end_time = rates.iloc[idx]["time"] + pd.Timedelta(hours=6)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    exit_price = float(rates.iloc[max(idx, end_idx - 1)]["close"])
    exit_idx = max(idx, end_idx - 1)
    status = "timeout"
    for bar_idx in range(idx, end_idx):
        bar = rates.iloc[bar_idx]
        high, low = float(bar["high"]), float(bar["low"])
        hit_sl = low <= current_sl if side == "buy" else high >= current_sl
        hit_tp = high >= tp if side == "buy" else low <= tp
        if hit_sl:
            exit_price = current_sl
            exit_idx = bar_idx
            status = "loss" if abs(current_sl - initial_sl) < 0.005 else "protected"
            break
        if hit_tp:
            exit_price = tp
            exit_idx = bar_idx
            status = "win"
            break
        advance = high - entry if side == "buy" else entry - low
        if be_trigger > 0 and advance >= be_trigger:
            protected = entry + be_buffer if side == "buy" else entry - be_buffer
            current_sl = max(current_sl, protected) if side == "buy" else min(current_sl, protected)
    net = _profit(symbol, side, 1.0, entry, exit_price) - spread_cost_1lot
    if status == "protected":
        status = "win" if net > 0.05 else ("loss" if net < -0.05 else "be")
    return {
        "status": status,
        "pnl": net,
        "entry_time": rates.iloc[idx]["time"].isoformat(),
        "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
        "entry": entry,
        "exit": exit_price,
    }


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row["status"]) for row in rows)
    gross_profit = sum(max(0.0, float(row["pnl"])) for row in rows)
    gross_loss = abs(sum(min(0.0, float(row["pnl"])) for row in rows))
    decided = counts["win"] + counts["loss"]
    return {
        "trades": len(rows),
        "wins": counts["win"],
        "losses": counts["loss"],
        "be": counts["be"],
        "timeouts": counts["timeout"],
        "win_rate_pct": round(100.0 * counts["win"] / max(1, decided), 2),
        "pnl_001": round(sum(float(row["pnl"]) for row in rows) * 0.01, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
    }


def _detail(
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
    outcome_cache: dict[tuple[int, float, float, float, float], dict[str, Any]],
) -> dict[str, Any]:
    key = (
        float(config["tp_usd"]),
        float(config["sl_usd"]),
        float(config["be_trigger_usd"]),
        float(config["be_buffer_usd"]),
    )
    rows = [
        (candidate, outcome_cache[(index, *key)])
        for index, candidate in enumerate(candidates)
        if float(candidate["delay_minutes"]) <= float(config["ttl_minutes"])
        and float(candidate["wrong_side_distance"]) <= float(config["tolerance_usd"])
        and (index, *key) in outcome_cache
    ]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    worst_loss_streak = 0
    monthly: dict[str, list[dict[str, Any]]] = {}
    for candidate, outcome in rows:
        pnl = float(outcome["pnl"]) * 0.01
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if str(outcome["status"]) == "loss":
            loss_streak += 1
            worst_loss_streak = max(worst_loss_streak, loss_streak)
        else:
            loss_streak = 0
        month = candidate["time"].strftime("%Y-%m")
        monthly.setdefault(month, []).append(outcome)
    return {
        "stats": _stats([outcome for _, outcome in rows]),
        "max_drawdown_001": round(max_drawdown, 2),
        "worst_loss_streak": worst_loss_streak,
        "monthly": {month: _stats(outcomes) for month, outcomes in monthly.items()},
    }


def _multi_leg_grid(
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    outcome_cache: dict[tuple[int, float, float, float, float], dict[str, Any]],
    split_index: int,
) -> list[dict[str, Any]]:
    results = []
    for targets in itertools.combinations_with_replacement((1.0, 1.5, 2.0, 2.5, 3.0), 3):
        rows: list[tuple[int, dict[str, Any]]] = []
        for index, candidate in enumerate(candidates):
            if float(candidate["delay_minutes"]) > float(profile["ttl_minutes"]):
                continue
            if float(candidate["wrong_side_distance"]) > float(profile["tolerance_usd"]):
                continue
            outcomes = [
                outcome_cache.get(
                    (
                        index,
                        target,
                        float(profile["sl_usd"]),
                        float(profile["be_trigger_usd"]),
                        float(profile["be_buffer_usd"]),
                    )
                )
                for target in targets
            ]
            if any(outcome is None for outcome in outcomes):
                continue
            pnl = sum(float(outcome["pnl"]) for outcome in outcomes if outcome is not None)
            status = "win" if pnl > 0.05 else ("loss" if pnl < -0.05 else "be")
            rows.append((index, {"status": status, "pnl": pnl}))
        train = _stats([row for index, row in rows if index < split_index])
        holdout = _stats([row for index, row in rows if index >= split_index])
        full = _stats([row for _, row in rows])
        results.append(
            {
                "targets_usd": list(targets),
                "train": train,
                "holdout": holdout,
                "full_per_leg_001": full,
                "full_same_total_volume_as_001_single_leg_pnl": round(full["pnl_001"] / 3.0, 2),
            }
        )
    results.sort(
        key=lambda row: (
            min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
            row["full_per_leg_001"]["pnl_001"],
        ),
        reverse=True,
    )
    return results


async def _messages(cfg: Any, start: datetime) -> list[dict[str, Any]]:
    session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
    client = TelegramClient(str(session), cfg.telegram_api_id, cfg.telegram_api_hash)
    output = []
    async with client:
        entity = await client.get_entity(CHANNEL_ID)
        async for message in client.iter_messages(entity):
            if message.date < start:
                break
            output.append(
                {
                    "id": int(message.id),
                    "time": message.date.astimezone(UTC),
                    "text": str(message.raw_text or ""),
                }
            )
    return sorted(output, key=lambda row: row["time"])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=98)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(".env.vantage", override=True)
    load_dotenv(".env.vantage.signal", override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        start = end - timedelta(days=max(2, int(args.days)))
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        rates = _rates(
            symbol,
            start + broker_offset - timedelta(days=2),
            end + broker_offset + timedelta(hours=1),
        )
        if rates.empty:
            raise RuntimeError("No M1 rates")
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        spread_price = float(rates["spread"].median()) * point
        spread_cost_1lot = abs(_profit(symbol, "buy", 1.0, 4000.0, 4000.0 + spread_price))
        messages = await _messages(cfg, start)

        candidates = []
        pending_side = ""
        pending_time: datetime | None = None
        pending_id = 0
        announcements = 0
        for message in messages:
            text = str(message["text"])
            if _is_phoenix_direction_runner_announcement(text):
                pending_side = _phoenix_direction_hint(text)
                pending_time = message["time"]
                pending_id = int(message["id"])
                announcements += 1
                continue
            match = RANGE_RE.match(text.replace(",", "."))
            if not match or not pending_side or pending_time is None:
                continue
            delay_minutes = (message["time"] - pending_time).total_seconds() / 60.0
            if delay_minutes < 0 or delay_minutes > 15.0:
                if delay_minutes > 15.0:
                    pending_side, pending_time, pending_id = "", None, 0
                continue
            values = sorted((float(match.group(1)), float(match.group(2))))
            candle_time = pd.Timestamp(message["time"] + broker_offset).ceil("1min")
            idx = int(rates["time"].searchsorted(candle_time, side="left"))
            if idx < len(rates):
                entry = float(rates.iloc[idx]["open"])
                wrong_side_distance = max(0.0, entry - values[1]) if pending_side == "buy" else max(0.0, values[0] - entry)
                candidates.append(
                    {
                        "direction_message_id": pending_id,
                        "range_message_id": int(message["id"]),
                        "time": message["time"],
                        "side": pending_side,
                        "low": values[0],
                        "high": values[1],
                        "delay_minutes": delay_minutes,
                        "wrong_side_distance": wrong_side_distance,
                        "start_idx": idx,
                        "entry": entry,
                    }
                )
            pending_side, pending_time, pending_id = "", None, 0

        protection_grid = [
            (0.0, 0.0),
            (0.5, 0.0),
            (0.5, 0.25),
            (0.75, 0.25),
            (1.0, 0.25),
            (1.0, 0.5),
            (1.5, 0.5),
        ]
        outcome_cache: dict[tuple[int, float, float, float, float], dict[str, Any]] = {}
        for candidate_index, candidate in enumerate(candidates):
            for tp in (1.0, 1.5, 2.0, 2.5, 3.0):
                for sl in SL_DISTANCES:
                    for be_trigger, be_buffer in protection_grid:
                        if be_trigger > 0 and (be_trigger >= tp or be_buffer >= be_trigger):
                            continue
                        outcome_cache[(candidate_index, tp, sl, be_trigger, be_buffer)] = _simulate(
                            candidate,
                            rates,
                            symbol,
                            spread_cost_1lot,
                            tp,
                            sl,
                            be_trigger,
                            be_buffer,
                        )

        split_index = max(1, int(len(candidates) * 0.60))
        grid = []
        for ttl in (2.0, 3.0, 5.0, 10.0, 15.0):
            for tolerance in (0.5, 1.0, 2.0, 3.0):
                for tp in (1.0, 1.5, 2.0, 2.5, 3.0):
                    for sl in SL_DISTANCES:
                        for be_trigger, be_buffer in protection_grid:
                            key_template = (tp, sl, be_trigger, be_buffer)
                            eligible = [
                                (index, outcome_cache[(index, *key_template)])
                                for index, candidate in enumerate(candidates)
                                if candidate["delay_minutes"] <= ttl
                                and candidate["wrong_side_distance"] <= tolerance
                                and (index, *key_template) in outcome_cache
                            ]
                            train = _stats([row for index, row in eligible if index < split_index])
                            holdout = _stats([row for index, row in eligible if index >= split_index])
                            full = _stats([row for _, row in eligible])
                            grid.append(
                                {
                                    "ttl_minutes": ttl,
                                    "tolerance_usd": tolerance,
                                    "tp_usd": tp,
                                    "sl_usd": sl,
                                    "be_trigger_usd": be_trigger,
                                    "be_buffer_usd": be_buffer,
                                    "train": train,
                                    "holdout": holdout,
                                    "full": full,
                                }
                            )

        robust = [
            row for row in grid
            if row["train"]["trades"] >= 25
            and row["holdout"]["trades"] >= 15
            and row["train"]["pnl_001"] > 0
            and row["holdout"]["pnl_001"] > 0
            and (row["train"]["profit_factor"] or 0.0) > 1.0
            and (row["holdout"]["profit_factor"] or 0.0) > 1.0
        ]
        robust.sort(
            key=lambda row: (
                min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
                row["full"]["pnl_001"],
                row["full"]["win_rate_pct"],
            ),
            reverse=True,
        )
        current = next(
            row for row in grid
            if row["ttl_minutes"] == 15.0
            and row["tolerance_usd"] == 2.0
            and row["tp_usd"] == 2.5
            and row["sl_usd"] == 6.0
            and row["be_trigger_usd"] == 0.5
            and row["be_buffer_usd"] == 0.25
        )
        winrate_candidates = [
            row for row in robust
            if min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0) >= 1.10
        ]
        max_winrate = max(
            winrate_candidates,
            key=lambda row: (row["full"]["win_rate_pct"], row["full"]["pnl_001"]),
            default=None,
        )
        max_profit = max(robust, key=lambda row: row["full"]["pnl_001"], default=None)
        no_be_reference = next(
            row for row in grid
            if row["ttl_minutes"] == 15.0
            and row["tolerance_usd"] == 2.0
            and row["tp_usd"] == 2.5
            and row["sl_usd"] == 6.0
            and row["be_trigger_usd"] == 0.0
            and row["be_buffer_usd"] == 0.0
        )
        sl_sensitivity = [
            next(
                row for row in grid
                if row["ttl_minutes"] == 15.0
                and row["tolerance_usd"] == 2.0
                and row["tp_usd"] == 2.5
                and row["sl_usd"] == sl_distance
                and row["be_trigger_usd"] == 0.5
                and row["be_buffer_usd"] == 0.25
            )
            for sl_distance in SL_DISTANCES
        ]
        selected_details = {
            "current": _detail(current, candidates, outcome_cache),
            "no_be_reference": _detail(no_be_reference, candidates, outcome_cache),
            "best_robust": _detail(robust[0], candidates, outcome_cache) if robust else None,
            "best_profit": _detail(max_profit, candidates, outcome_cache) if max_profit else None,
        }
        current_live_three_leg_events = []
        for candidate_index, candidate in enumerate(candidates):
            if float(candidate["delay_minutes"]) > 15.0:
                continue
            if float(candidate["wrong_side_distance"]) > 2.0:
                continue
            for leg_index, target_distance in enumerate((1.25, 2.0, 5.0), start=1):
                outcome = _simulate(
                    candidate,
                    rates,
                    symbol,
                    spread_cost_1lot,
                    target_distance,
                    6.0,
                    0.5,
                    0.25,
                )
                current_live_three_leg_events.append(
                    {
                        **outcome,
                        "source": "phoenix_range",
                        "direction_message_id": int(candidate["direction_message_id"]),
                        "range_message_id": int(candidate["range_message_id"]),
                        "signal_time": candidate["time"].isoformat(),
                        "side": str(candidate["side"]),
                        "target_distance": target_distance,
                        "leg_index": leg_index,
                    }
                )
        current_multi_leg = _multi_leg_grid(current, candidates, outcome_cache, split_index)
        robust_multi_leg = _multi_leg_grid(robust[0], candidates, outcome_cache, split_index) if robust else []
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": start.isoformat(), "end": end.isoformat()},
            "method": "all first bare numeric ranges after Phoenix direction announcements; no later full signal required; broker-time aligned next M1 open; SL-first intrabar; spread deducted",
            "broker_offset_hours": float(args.broker_offset_hours),
            "messages": len(messages),
            "direction_announcements": announcements,
            "range_sequences": len(candidates),
            "spread_price": spread_price,
            "split_index": split_index,
            "tested_configs": len(grid),
            "robust_configs": len(robust),
            "current_config": current,
            "no_be_reference": no_be_reference,
            "best_robust": robust[0] if robust else None,
            "best_winrate": max_winrate,
            "best_profit": max_profit,
            "selected_details": selected_details,
            "current_live_three_leg_events": current_live_three_leg_events,
            "sl_sensitivity_current_filter": sl_sensitivity,
            "three_leg_tests": {
                "current_filter_top10": current_multi_leg[:10],
                "best_robust_filter_top10": robust_multi_leg[:10],
            },
            "top20": robust[:20],
            "candidates": [
                {**row, "time": row["time"].isoformat()}
                for row in candidates
            ],
        }
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({key: output[key] for key in ("messages", "direction_announcements", "range_sequences", "tested_configs", "robust_configs", "current_config", "best_robust", "best_winrate", "best_profit")}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
