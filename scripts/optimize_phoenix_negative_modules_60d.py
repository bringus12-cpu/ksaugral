from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _repair_tps_for_entry, _select_live_tps
from backtest_phoenix_complete_60d import _fetch_history, _rates


def _first_true(mask: np.ndarray) -> int:
    found = np.flatnonzero(mask)
    return int(found[0]) if len(found) else -1


def _summary(values: list[float]) -> dict[str, Any]:
    positive = sum(1 for value in values if value > 0.0)
    negative = sum(1 for value in values if value < 0.0)
    gross_win = sum(max(0.0, value) for value in values)
    gross_loss = abs(sum(min(0.0, value) for value in values))
    return {
        "samples": len(values),
        "positive": positive,
        "negative": negative,
        "win_rate_pct": round(100.0 * positive / max(1, positive + negative), 2),
        "pnl": round(sum(values), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


def _direction_outcome(
    highs: np.ndarray,
    lows: np.ndarray,
    start_idx: int,
    side: str,
    entry: float,
    target_distance: float,
    stop_distance: float,
    value_per_usd: float,
    lot: float,
    spread_price: float,
) -> float:
    end_idx = min(start_idx + 360, len(highs))
    high = highs[start_idx:end_idx]
    low = lows[start_idx:end_idx]
    if side == "buy":
        tp_idx = _first_true(high >= entry + target_distance)
        sl_idx = _first_true(low <= entry - stop_distance)
    else:
        tp_idx = _first_true(low <= entry - target_distance)
        sl_idx = _first_true(high >= entry + stop_distance)
    win = tp_idx >= 0 and (sl_idx < 0 or tp_idx < sl_idx)
    move = target_distance if win else -stop_distance
    return round(move * value_per_usd * lot - spread_price * value_per_usd * lot, 2)


def _matrix_outcome(
    *,
    highs: np.ndarray,
    lows: np.ndarray,
    start_idx: int,
    side: str,
    entry: float,
    signal_sl: float,
    tp1: float,
    target: float,
    cancel_target: float,
    protect_be: bool,
    stop_cap: float,
    expiry_minutes: int,
    cancel_tolerance: float,
    value_per_usd: float,
    lot: float,
    spread_price: float,
) -> float | None:
    stop = float(signal_sl)
    if side == "buy" and stop >= entry:
        stop = entry - stop_cap
    elif side == "sell" and stop <= entry:
        stop = entry + stop_cap
    if abs(entry - stop) > stop_cap:
        stop = entry - stop_cap if side == "buy" else entry + stop_cap

    expiry_idx = min(start_idx + int(expiry_minutes) + 1, len(highs))
    trigger_idx = -1
    for idx in range(start_idx, expiry_idx):
        favorable = highs[idx] if side == "buy" else lows[idx]
        cancel = favorable >= cancel_target - cancel_tolerance if side == "buy" else favorable <= cancel_target + cancel_tolerance
        if cancel:
            return None
        if lows[idx] <= entry <= highs[idx]:
            trigger_idx = idx
            break
    if trigger_idx < 0:
        return None

    current_stop = stop
    end_idx = min(trigger_idx + 360, len(highs))
    for idx in range(trigger_idx, end_idx):
        hit_sl = lows[idx] <= current_stop if side == "buy" else highs[idx] >= current_stop
        if hit_sl:
            move = current_stop - entry if side == "buy" else entry - current_stop
            return round(move * value_per_usd * lot - spread_price * value_per_usd * lot, 2)
        hit_target = highs[idx] >= target if side == "buy" else lows[idx] <= target
        if hit_target:
            move = target - entry if side == "buy" else entry - target
            return round(move * value_per_usd * lot - spread_price * value_per_usd * lot, 2)
        hit_tp1 = highs[idx] >= tp1 if side == "buy" else lows[idx] <= tp1
        if protect_be and hit_tp1:
            current_stop = max(current_stop, entry) if side == "buy" else min(current_stop, entry)

    close = (highs[end_idx - 1] + lows[end_idx - 1]) / 2.0
    move = close - entry if side == "buy" else entry - close
    return round(move * value_per_usd * lot - spread_price * value_per_usd * lot, 2)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--lot", type=float, default=0.10)
    parser.add_argument("--direction-only", action="store_true")
    parser.add_argument("--output", default="data_vantage/optimize_phoenix_negative_modules_60d.json")
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        cutoff = end - timedelta(days=int(args.days))
        rates = _rates(symbol, cutoff - timedelta(days=1), end + timedelta(hours=1))
        highs = rates["high"].to_numpy(dtype=float)
        lows = rates["low"].to_numpy(dtype=float)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        spread_price = float(rates["spread"].median()) * point
        anchor = float(rates.iloc[-1]["close"])
        buy_value = abs(float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, anchor, anchor + 1.0) or 0.0))
        sell_value = abs(float(mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol, 1.0, anchor, anchor - 1.0) or 0.0))
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, announcements = await _fetch_history(rates, cutoff, session)
        rates_start = rates.iloc[0]["time"].to_pydatetime()
        rates_end = rates.iloc[-1]["time"].to_pydatetime()
        signals = [item for item in signals if rates_start <= item.time <= rates_end]
        announcements = [
            item for item in announcements
            if rates_start <= item["time"] <= rates_end
        ]

        direction_rows = []
        direction_targets = (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
        direction_stops = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
        direction_cache: dict[tuple[int, float, float], float] = {}
        for index, item in enumerate(announcements):
            start_idx = int(rates["time"].searchsorted(pd.Timestamp(item["time"]).ceil("min"), side="left"))
            if start_idx >= len(rates):
                continue
            entry = float(rates.iloc[start_idx]["open"])
            value = buy_value if item["side"] == "buy" else sell_value
            for target in direction_targets:
                for stop in direction_stops:
                    direction_cache[(index, target, stop)] = _direction_outcome(
                        highs, lows, start_idx, str(item["side"]), entry, target, stop,
                        value, float(args.lot), spread_price,
                    )
        direction_split = max(1, int(len(announcements) * 0.60))
        single_direction_rows = []
        for target in direction_targets:
            for stop in direction_stops:
                by_signal = [
                    direction_cache[(index, target, stop)]
                    for index in range(len(announcements))
                ]
                single_direction_rows.append(
                    {
                        "target_usd": target,
                        "stop_usd": stop,
                        "train": _summary(by_signal[:direction_split]),
                        "holdout": _summary(by_signal[direction_split:]),
                        "full": _summary(by_signal),
                    }
                )
        single_direction_robust = [
            row for row in single_direction_rows
            if row["train"]["pnl"] > 0 and row["holdout"]["pnl"] > 0
        ]
        single_direction_robust.sort(
            key=lambda row: (
                min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
                row["full"]["pnl"],
            ),
            reverse=True,
        )
        single_direction_by_full = sorted(
            single_direction_rows,
            key=lambda row: row["full"]["pnl"],
            reverse=True,
        )
        single_direction_by_side: dict[str, dict[str, Any]] = {}
        for tested_side in ("buy", "sell"):
            side_indices = [
                index for index, item in enumerate(announcements)
                if item["side"] == tested_side
            ]
            side_split = max(1, int(len(side_indices) * 0.60))
            side_rows = []
            for target in direction_targets:
                for stop in direction_stops:
                    values = [
                        direction_cache[(index, target, stop)]
                        for index in side_indices
                    ]
                    side_rows.append(
                        {
                            "target_usd": target,
                            "stop_usd": stop,
                            "train": _summary(values[:side_split]),
                            "holdout": _summary(values[side_split:]),
                            "full": _summary(values),
                        }
                    )
            side_robust = [
                row for row in side_rows
                if row["train"]["pnl"] > 0 and row["holdout"]["pnl"] > 0
            ]
            side_robust.sort(
                key=lambda row: (
                    min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
                    row["full"]["pnl"],
                ),
                reverse=True,
            )
            side_by_full = sorted(side_rows, key=lambda row: row["full"]["pnl"], reverse=True)
            single_direction_by_side[tested_side] = {
                "announcements": len(side_indices),
                "tested_configs": len(side_rows),
                "robust_configs": len(side_robust),
                "best": side_robust[0] if side_robust else None,
                "best_full_regardless_of_holdout": side_by_full[0] if side_by_full else None,
            }
        for targets in itertools.combinations(direction_targets, 3):
            for stop in direction_stops:
                by_signal = [
                    round(sum(direction_cache[(index, target, stop)] for target in targets), 2)
                    for index in range(len(announcements))
                ]
                train = _summary(by_signal[:direction_split])
                holdout = _summary(by_signal[direction_split:])
                full = _summary(by_signal)
                direction_rows.append({"targets_usd": targets, "stop_usd": stop, "train": train, "holdout": holdout, "full": full})
        direction_robust = [row for row in direction_rows if row["train"]["pnl"] > 0 and row["holdout"]["pnl"] > 0]
        direction_robust.sort(
            key=lambda row: (
                min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
                row["full"]["pnl"],
                row["holdout"]["pnl"],
            ),
            reverse=True,
        )
        if args.direction_only:
            output = {
                "generated_utc": datetime.now(UTC).isoformat(),
                "days": int(args.days),
                "rates_start_utc": rates_start.isoformat(),
                "rates_end_utc": rates_end.isoformat(),
                "timeframe": str(os.getenv("PHOENIX_BACKTEST_TIMEFRAME", "M1") or "M1").upper(),
                "direction_announcements": len(announcements),
                "lot_per_leg": float(args.lot),
                "median_spread_usd": round(spread_price, 4),
                "validation": "chronological 60% train / 40% holdout; selected rows profitable in both",
                "direction": {
                    "tested_configs": len(direction_rows),
                    "robust_configs": len(direction_robust),
                    "best": direction_robust[0] if direction_robust else None,
                    "best_full_regardless_of_holdout": max(
                        direction_rows,
                        key=lambda row: row["full"]["pnl"],
                        default=None,
                    ),
                    "top10": direction_robust[:10],
                },
                "direction_single_leg": {
                    "tested_configs": len(single_direction_rows),
                    "robust_configs": len(single_direction_robust),
                    "best": single_direction_robust[0] if single_direction_robust else None,
                    "best_full_regardless_of_holdout": single_direction_by_full[0] if single_direction_by_full else None,
                    "top10_full_regardless_of_holdout": single_direction_by_full[:10],
                },
                "direction_single_leg_by_side": single_direction_by_side,
            }
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
            print(json.dumps(output["direction"], indent=2))
            return

        confirmed_rows: list[dict[str, Any]] = []
        confirmed_setups: list[tuple[int, str]] = []
        for item in signals:
            prior = [
                announcement for announcement in announcements
                if announcement["side"] == item.signal.side
                and timedelta(0) <= item.time - announcement["time"] <= timedelta(minutes=15)
            ]
            if not prior:
                continue
            entries = [float(value) for value in item.signal.entries if float(value or 0.0) > 0]
            if len(entries) < 2:
                continue
            low, high = min(entries), max(entries)
            market_ok = item.market <= high + 2.0 if item.signal.side == "buy" else item.market >= low - 2.0
            if market_ok:
                confirmed_setups.append((item.start_idx, item.signal.side))
        confirmed_split = max(1, int(len(confirmed_setups) * 0.60))
        for targets in itertools.combinations(direction_targets, 3):
            for stop in direction_stops:
                by_signal = []
                for start_idx, side in confirmed_setups:
                    entry = float(rates.iloc[start_idx]["open"])
                    value = buy_value if side == "buy" else sell_value
                    by_signal.append(
                        round(
                            sum(
                                _direction_outcome(
                                    highs, lows, start_idx, side, entry, target, stop,
                                    value, float(args.lot), spread_price,
                                )
                                for target in targets
                            ),
                            2,
                        )
                    )
                train = _summary(by_signal[:confirmed_split])
                holdout = _summary(by_signal[confirmed_split:])
                full = _summary(by_signal)
                confirmed_rows.append({"targets_usd": targets, "stop_usd": stop, "train": train, "holdout": holdout, "full": full})
        confirmed_robust = [row for row in confirmed_rows if row["train"]["pnl"] > 0 and row["holdout"]["pnl"] > 0]
        confirmed_robust.sort(
            key=lambda row: (
                min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
                row["full"]["pnl"],
                row["holdout"]["pnl"],
            ),
            reverse=True,
        )

        matrix_rows = []
        target_plans = list(itertools.combinations((1, 2, 3, 4, 5, 6), 3))
        stop_caps = (2.5, 3.0, 4.0, 5.0, 6.0)
        expiries = (5, 8, 15, 30)
        cancel_indices = (1, 2, 3)
        matrix_split = max(1, int(len(signals) * 0.60))
        matrix_cache: dict[tuple[int, int, float, int, int, bool], float | None] = {}
        level_cache: dict[tuple[int, int], tuple[float, float, float] | None] = {}
        midpoint_cache: dict[int, float] = {}
        for signal_index, item in enumerate(signals):
            entries = [float(value) for value in item.signal.entries if float(value or 0.0) > 0]
            if len(entries) < 2:
                continue
            midpoint_cache[signal_index] = sorted(entries)[len(entries) // 2]
            entry = midpoint_cache[signal_index]
            for target_index in range(1, 7):
                try:
                    repaired = _repair_tps_for_entry(item.signal.side, entry, item.signal.tps, max(target_index, 6))
                    tp1, target, _, _ = _select_live_tps(item.signal.side, entry, repaired, target_index)
                    level_cache[(signal_index, target_index)] = (tp1, target, repaired[0])
                except Exception:
                    level_cache[(signal_index, target_index)] = None

        for plan in target_plans:
            for stop_cap in stop_caps:
                for expiry in expiries:
                    for cancel_index in cancel_indices:
                        by_signal: list[float] = []
                        entered = 0
                        for signal_index, item in enumerate(signals):
                            entry = midpoint_cache.get(signal_index)
                            if entry is None:
                                by_signal.append(0.0)
                                continue
                            signal_pnl = 0.0
                            for branch, target_index in enumerate(plan):
                                protect = branch > 0
                                key = (signal_index, target_index, stop_cap, expiry, cancel_index, protect)
                                if key not in matrix_cache:
                                    try:
                                        repaired = _repair_tps_for_entry(item.signal.side, entry, item.signal.tps, max(target_index, cancel_index, 6))
                                        tp1, target, _, _ = _select_live_tps(item.signal.side, entry, repaired, target_index)
                                        cancel_target = repaired[min(cancel_index - 1, len(repaired) - 1)]
                                        value = buy_value if item.signal.side == "buy" else sell_value
                                        matrix_cache[key] = _matrix_outcome(
                                            highs=highs,
                                            lows=lows,
                                            start_idx=item.start_idx,
                                            side=item.signal.side,
                                            entry=entry,
                                            signal_sl=float(item.signal.sl or 0.0),
                                            tp1=tp1,
                                            target=target,
                                            cancel_target=cancel_target,
                                            protect_be=protect,
                                            stop_cap=stop_cap,
                                            expiry_minutes=expiry,
                                            cancel_tolerance=0.25,
                                            value_per_usd=value,
                                            lot=float(args.lot),
                                            spread_price=spread_price,
                                        )
                                    except Exception:
                                        matrix_cache[key] = None
                                outcome = matrix_cache[key]
                                if outcome is not None:
                                    signal_pnl += outcome
                                    entered += 1
                            by_signal.append(round(signal_pnl, 2))
                        train = _summary(by_signal[:matrix_split])
                        holdout = _summary(by_signal[matrix_split:])
                        full = _summary(by_signal)
                        matrix_rows.append(
                            {
                                "targets": plan,
                                "stop_cap_usd": stop_cap,
                                "pending_expiry_minutes": expiry,
                                "cancel_at_tp": cancel_index,
                                "entered_legs": entered,
                                "train": train,
                                "holdout": holdout,
                                "full": full,
                            }
                        )
        matrix_robust = [
            row for row in matrix_rows
            if row["train"]["pnl"] > 0
            and row["holdout"]["pnl"] > 0
            and int(row["entered_legs"]) >= 90
        ]
        matrix_robust.sort(
            key=lambda row: (
                min(row["train"]["profit_factor"] or 0.0, row["holdout"]["profit_factor"] or 0.0),
                row["full"]["pnl"],
                row["holdout"]["pnl"],
            ),
            reverse=True,
        )

        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "days": int(args.days),
            "rates_start_utc": rates_start.isoformat(),
            "rates_end_utc": rates_end.isoformat(),
            "timeframe": str(os.getenv("PHOENIX_BACKTEST_TIMEFRAME", "M1") or "M1").upper(),
            "signals": len(signals),
            "direction_announcements": len(announcements),
            "lot_per_leg": float(args.lot),
            "median_spread_usd": round(spread_price, 4),
            "validation": "chronological 60% train / 40% holdout; selected rows profitable in both",
            "direction": {
                "tested_configs": len(direction_rows),
                "robust_configs": len(direction_robust),
                "best": direction_robust[0] if direction_robust else None,
                "top10": direction_robust[:10],
            },
            "direction_confirmed_by_zone": {
                "rule": "wait for a full range signal within 15 minutes; current price must remain inside/within 2 USD of the directional side of the range",
                "setups": len(confirmed_setups),
                "tested_configs": len(confirmed_rows),
                "robust_configs": len(confirmed_robust),
                "best": confirmed_robust[0] if confirmed_robust else None,
                "top10": confirmed_robust[:10],
            },
            "middle_entry_e2": {
                "tested_configs": len(matrix_rows),
                "robust_configs": len(matrix_robust),
                "best": matrix_robust[0] if matrix_robust else None,
                "top10": matrix_robust[:10],
            },
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({
            "direction": output["direction"],
            "direction_confirmed_by_zone": output["direction_confirmed_by_zone"],
            "middle_entry_e2": output["middle_entry_e2"],
        }, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
