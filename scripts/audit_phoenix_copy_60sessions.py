from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_phoenix_active_profile import _completed_sessions
from scripts.backtest_phoenix_active_range import _ranges
from scripts.backtest_phoenix_complete_60d import _fetch_history, _rates


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    relative = gain / loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + relative)).fillna(50.0)


def _features(rates: pd.DataFrame) -> pd.DataFrame:
    frame = rates.copy()
    close = frame["close"].astype(float)
    frame["ema9"] = _ema(close, 9)
    frame["ema21"] = _ema(close, 21)
    macd = _ema(close, 12) - _ema(close, 26)
    frame["macd_hist"] = macd - _ema(macd, 9)
    frame["mom3"] = close.diff(3)
    frame["mom10"] = close.diff(10)
    frame["rsi14"] = _rsi(close)
    frame["candle"] = close - frame["open"].astype(float)
    return frame


def _gate_flags(row: pd.Series, side: str) -> dict[str, bool]:
    direction = 1.0 if side == "buy" else -1.0
    flags = {
        "ema": direction * (float(row["ema9"]) - float(row["ema21"])) > 0.0,
        "macd": direction * float(row["macd_hist"]) > 0.0,
        "mom3": direction * float(row["mom3"]) > 0.0,
        "mom10": direction * float(row["mom10"]) > 0.0,
        "rsi": direction * (float(row["rsi14"]) - 50.0) > 0.0,
        "candle": direction * float(row["candle"]) > 0.0,
    }
    score = sum(flags[name] for name in ("ema", "macd", "mom3", "mom10"))
    return {
        "all": True,
        **flags,
        "ema_macd": flags["ema"] and flags["macd"],
        "ema_mom3": flags["ema"] and flags["mom3"],
        "majority2": score >= 2,
        "majority3": score >= 3,
        "majority4": score >= 4,
        "pullback": flags["ema"] and not flags["mom3"],
    }


def _simulate_raw(
    rates: pd.DataFrame,
    start_idx: int,
    side: str,
    target_distance: float,
    stop_distance: float,
    horizon_minutes: int,
    point: float,
    commission: float,
) -> dict[str, Any] | None:
    if start_idx < 0 or start_idx >= len(rates):
        return None
    first = rates.iloc[start_idx]
    first_spread = float(first["spread"]) * point
    bid = float(first["open"])
    entry = bid + first_spread if side == "buy" else bid
    target = entry + target_distance if side == "buy" else entry - target_distance
    stop = entry - stop_distance if side == "buy" else entry + stop_distance
    end_idx = min(len(rates), start_idx + max(1, int(horizon_minutes)))
    exit_idx = max(start_idx, end_idx - 1)
    exit_price = float(rates.iloc[exit_idx]["close"])
    if side == "sell":
        exit_price += float(rates.iloc[exit_idx]["spread"]) * point
    status = "timeout"
    for index in range(start_idx, end_idx):
        bar = rates.iloc[index]
        spread = float(bar["spread"]) * point
        high = float(bar["high"])
        low = float(bar["low"])
        exit_high = high if side == "buy" else high + spread
        exit_low = low if side == "buy" else low + spread
        hit_stop = exit_low <= stop if side == "buy" else exit_high >= stop
        hit_target = exit_high >= target if side == "buy" else exit_low <= target
        if hit_stop:
            status, exit_idx, exit_price = "stop", index, stop
            break
        if hit_target:
            status, exit_idx, exit_price = "target", index, target
            break
    signed = exit_price - entry if side == "buy" else entry - exit_price
    return {
        "status": status,
        "pnl_001": signed - commission,
        "entry": entry,
        "exit": exit_price,
        "exit_idx": exit_idx,
    }


def _stats(values: list[float]) -> dict[str, Any]:
    wins = sum(value > 0.0 for value in values)
    losses = sum(value < 0.0 for value in values)
    gross_win = sum(max(0.0, value) for value in values)
    gross_loss = abs(sum(min(0.0, value) for value in values))
    equity = peak = max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return {
        "trades": len(values),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "pnl_001": round(sum(values), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_001": round(max_drawdown, 2),
    }


def _zone_state(side: str, market: float, entries: list[float], tp1: float) -> str:
    low, high = min(entries), max(entries)
    if low <= market <= high:
        return "inside_zone"
    if side == "buy":
        if market > high:
            return "after_tp1" if market >= tp1 else "after_zone_before_tp1"
        return "before_zone"
    if market < low:
        return "after_tp1" if market <= tp1 else "after_zone_before_tp1"
    return "before_zone"


def _percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    return {
        "p25": round(float(np.percentile(array, 25)), 2),
        "median": round(float(np.percentile(array, 50)), 2),
        "p75": round(float(np.percentile(array, 75)), 2),
        "p90": round(float(np.percentile(array, 90)), 2),
        "max": round(float(array.max()), 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/phoenix_copy_audit_60sessions_20260810.json")
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(args.broker_offset_hours)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        warmup_start = end - timedelta(days=max(130, int(args.sessions * 2.2)))
        all_rates = _features(_rates(symbol, warmup_start, end + timedelta(hours=1)))
        session_dates, cutoff = _completed_sessions(all_rates, end, int(args.sessions))
        analysis_rates = all_rates[all_rates["time"] >= pd.Timestamp(cutoff) - pd.Timedelta(days=2)].reset_index(drop=True)
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, announcements = await _fetch_history(analysis_rates, cutoff, session)
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        ranges = await _ranges(cfg, cutoff, end, broker_offset)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)

        signal_rows = sorted(signals, key=lambda item: item.time)
        range_rows = sorted(ranges, key=lambda item: item["time"])
        announcement_rows = sorted(
            [item for item in announcements if cutoff <= item["time"] + broker_offset < end],
            key=lambda item: item["time"],
        )

        range_delays: list[float] = []
        full_delays: list[float] = []
        paired_ranges = 0
        paired_full = 0
        sequence_rows: list[dict[str, Any]] = []
        raw_events: list[dict[str, Any]] = []
        for announcement in announcement_rows:
            side = str(announcement["side"])
            broker_time = announcement["time"] + broker_offset
            start_idx = int(analysis_rates["time"].searchsorted(pd.Timestamp(broker_time).ceil("min"), side="left"))
            feature_idx = start_idx - 1
            if feature_idx < 30 or start_idx >= len(analysis_rates):
                continue
            matching_range = next(
                (
                    row
                    for row in range_rows
                    if row["side"] == side
                    and timedelta(0) <= row["time"] - broker_time <= timedelta(minutes=15)
                ),
                None,
            )
            matching_full = next(
                (
                    item
                    for item in signal_rows
                    if item.signal.side == side
                    and timedelta(0) <= item.time - announcement["time"] <= timedelta(minutes=20)
                ),
                None,
            )
            row: dict[str, Any] = {
                "announcement_id": int(announcement["message_id"]),
                "time": announcement["time"].isoformat(),
                "side": side,
                "market": round(float(analysis_rates.iloc[start_idx]["open"]), 3),
                "gates": _gate_flags(analysis_rates.iloc[feature_idx], side),
            }
            if matching_range is not None:
                delay = (matching_range["time"] - broker_time).total_seconds()
                paired_ranges += 1
                range_delays.append(delay)
                row.update({"range_id": int(matching_range["message_id"]), "range_delay_seconds": delay})
            if matching_full is not None:
                delay = (matching_full.time - announcement["time"]).total_seconds()
                paired_full += 1
                full_delays.append(delay)
                row.update({"full_id": int(matching_full.message_id), "full_delay_seconds": delay})
            sequence_rows.append(row)
            raw_events.append({"start_idx": start_idx, "side": side, "gates": row["gates"]})

        split_train = int(len(raw_events) * 0.50)
        split_validation = int(len(raw_events) * 0.75)
        outcome_cache: dict[tuple[int, float, float, int], float] = {}
        for event_index, event in enumerate(raw_events):
            for target in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5):
                for stop in (3.0, 4.0, 6.0, 8.0, 10.0, 12.0):
                    for horizon in (15, 30, 60):
                        outcome = _simulate_raw(
                            analysis_rates,
                            int(event["start_idx"]),
                            str(event["side"]),
                            target,
                            stop,
                            horizon,
                            point,
                            float(args.commission_per_001),
                        )
                        if outcome is not None:
                            outcome_cache[(event_index, target, stop, horizon)] = float(outcome["pnl_001"])
        grid: list[dict[str, Any]] = []
        for gate in ("all", "ema", "macd", "mom3", "mom10", "rsi", "candle", "ema_macd", "ema_mom3", "majority2", "majority3", "majority4", "pullback"):
            for target in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5):
                for stop in (3.0, 4.0, 6.0, 8.0, 10.0, 12.0):
                    for horizon in (15, 30, 60):
                        train: list[float] = []
                        validation: list[float] = []
                        test: list[float] = []
                        for event_index, event in enumerate(raw_events):
                            if not bool(event["gates"][gate]):
                                continue
                            pnl = outcome_cache.get((event_index, target, stop, horizon))
                            if pnl is None:
                                continue
                            if event_index < split_train:
                                train.append(pnl)
                            elif event_index < split_validation:
                                validation.append(pnl)
                            else:
                                test.append(pnl)
                        train_stats = _stats(train)
                        validation_stats = _stats(validation)
                        test_stats = _stats(test)
                        full_stats = _stats(train + validation + test)
                        selected_on_development = (
                            train_stats["trades"] >= 20
                            and validation_stats["trades"] >= 10
                            and train_stats["pnl_001"] > 0.0
                            and validation_stats["pnl_001"] > 0.0
                            and (train_stats["profit_factor"] or 0.0) > 1.05
                            and (validation_stats["profit_factor"] or 0.0) > 1.05
                        )
                        grid.append(
                            {
                                "gate": gate,
                                "target_usd": target,
                                "stop_usd": stop,
                                "horizon_minutes": horizon,
                                "selected_on_development": selected_on_development,
                                "train": train_stats,
                                "validation": validation_stats,
                                "untouched_test": test_stats,
                                "full": full_stats,
                            }
                        )
        development_rows = [row for row in grid if row["selected_on_development"]]
        development_rows.sort(
            key=lambda row: (
                min(row["train"]["profit_factor"] or 0.0, row["validation"]["profit_factor"] or 0.0),
                row["train"]["pnl_001"] + row["validation"]["pnl_001"],
                row["train"]["trades"] + row["validation"]["trades"],
            ),
            reverse=True,
        )
        selected = development_rows[0] if development_rows else None
        deployment_ready = bool(
            selected
            and selected["untouched_test"]["trades"] >= 10
            and selected["untouched_test"]["pnl_001"] > 0.0
            and (selected["untouched_test"]["profit_factor"] or 0.0) > 1.05
        )

        zone_counts: Counter[str] = Counter()
        explicit_side = 0
        inferred_side = 0
        revisions = 0
        previous = None
        signal_detail: list[dict[str, Any]] = []
        for item in signal_rows:
            text_upper = str(item.signal.raw_text or "").upper()
            if any(word in text_upper for word in ("BUY", "SELL", "LONG", "SHORT")):
                explicit_side += 1
            else:
                inferred_side += 1
            entries = [float(value) for value in item.signal.entries if float(value or 0.0) > 0.0]
            tps = [float(value) for value in item.signal.tps if float(value or 0.0) > 0.0]
            state = _zone_state(item.signal.side, float(item.market), entries, tps[0])
            zone_counts[state] += 1
            if previous is not None:
                close_time = item.time - previous.time <= timedelta(seconds=120)
                same_side = item.signal.side == previous.signal.side
                previous_tps = [float(value) for value in previous.signal.tps if float(value or 0.0) > 0.0]
                same_tp1 = previous_tps and abs(tps[0] - previous_tps[0]) <= 0.10
                if close_time and same_side and same_tp1:
                    revisions += 1
            previous = item
            signal_detail.append(
                {
                    "message_id": int(item.message_id),
                    "time": item.time.isoformat(),
                    "side": item.signal.side,
                    "market": round(float(item.market), 3),
                    "entries": entries,
                    "sl": float(item.signal.sl or 0.0),
                    "tp1": tps[0],
                    "zone_state": state,
                }
            )

        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions": session_dates,
            "symbol": symbol,
            "method": (
                "Phoenix Telegram chronology aligned to broker M1 (+3h); raw direction uses only the preceding "
                "closed M1 candle; chronological 60/40 split; spread reconstructed per bar; commission included; "
                "SL-first on ambiguous bars"
            ),
            "sequence": {
                "direction_announcements": len(announcement_rows),
                "numeric_ranges": len(range_rows),
                "full_signals": len(signal_rows),
                "announcements_paired_to_range": paired_ranges,
                "announcements_paired_to_full_signal": paired_full,
                "range_delay_seconds": _percentiles(range_delays),
                "full_signal_delay_seconds": _percentiles(full_delays),
            },
            "copy_risks": {
                "full_signals_with_explicit_side": explicit_side,
                "full_signals_with_inferred_side": inferred_side,
                "rapid_followup_revisions": revisions,
                "zone_state_at_full_publish": dict(zone_counts),
            },
            "raw_direction_validation": {
                "events": len(raw_events),
                "tested_configs": len(grid),
                "development_candidates": len(development_rows),
                "selected_without_looking_at_final_test": selected,
                "untouched_test_passed": deployment_ready,
                "top10_development": development_rows[:10],
                "best_unfiltered": max(
                    (row for row in grid if row["gate"] == "all"),
                    key=lambda row: (row["full"]["pnl_001"], row["full"]["profit_factor"] or 0.0),
                ),
            },
            "sequences": sequence_rows,
            "signals": signal_detail,
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(output_path),
                    "range": output["range_utc"],
                    "sequence": output["sequence"],
                    "copy_risks": output["copy_risks"],
                    "raw_direction_validation": output["raw_direction_validation"],
                },
                indent=2,
            )
        )
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
