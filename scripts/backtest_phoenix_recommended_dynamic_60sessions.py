from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.audit_phoenix_copy_60sessions import _simulate_raw
from scripts.backtest_phoenix_complete_60d import _rates


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _event(row: dict[str, Any], source: str, *, copy_index: int = 1) -> dict[str, Any]:
    return {
        "source": source,
        "message_id": int(row["message_id"]),
        "copy_index": int(copy_index),
        "entry_time": _dt(row["entry_time"]),
        "exit_time": _dt(row["exit_time"]),
        "side": str(row["side"]),
        "entry": float(row.get("entry", 0.0) or 0.0),
        "sl": float(row.get("sl", 0.0) or 0.0),
        "pnl_001": float(row["pnl_001"]),
        "status": str(row.get("status", "unknown")),
    }


def _core_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in report.get("trades", []):
        target_index = int(row.get("target_index", 0))
        if target_index == 1:
            events.append(_event(row, "phoenix_full_tp1", copy_index=1))
            events.append(_event(row, "phoenix_full_tp1", copy_index=2))
        elif target_index == 2:
            events.append(_event(row, "phoenix_full_tp2"))
    return events


def _range_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [_event(row, "phoenix_range") for row in report.get("trades", [])]


def _direction_events(
    *,
    audit: dict[str, Any],
    rates: pd.DataFrame,
    point: float,
    stop_usd: float,
    horizon_minutes: int,
    broker_offset_hours: float,
    commission_per_001: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    broker_offset = timedelta(hours=float(broker_offset_hours))
    for row in audit.get("sequences", []):
        if not bool(row.get("gates", {}).get("pullback", False)):
            continue
        announcement_time = _dt(row["time"])
        broker_time = announcement_time + broker_offset
        start_idx = int(rates["time"].searchsorted(pd.Timestamp(broker_time).ceil("min"), side="left"))
        outcome = _simulate_raw(
            rates,
            start_idx,
            str(row["side"]),
            1.0,
            float(stop_usd),
            int(horizon_minutes),
            float(point),
            float(commission_per_001),
        )
        if outcome is None:
            continue
        exit_idx = int(outcome["exit_idx"])
        events.append(
            {
                "source": "phoenix_direction_pullback",
                "message_id": int(row["announcement_id"]),
                "copy_index": 1,
                "entry_time": _dt(rates.iloc[start_idx]["time"].isoformat()),
                "exit_time": _dt(rates.iloc[exit_idx]["time"].isoformat()),
                "side": str(row["side"]),
                "entry": float(outcome["entry"]),
                "sl": (
                    float(outcome["entry"]) - float(stop_usd)
                    if str(row["side"]) == "buy"
                    else float(outcome["entry"]) + float(stop_usd)
                ),
                "pnl_001": float(outcome["pnl_001"]),
                "status": str(outcome["status"]),
            }
        )
    return events


def _lot(balance: float, start_balance: float, step_usd: float, step_lot: float) -> float:
    earned_steps = math.floor(max(0.0, balance - start_balance + 1e-9) / step_usd)
    return round(0.01 + earned_steps * step_lot, 2)


def _simulate(
    events: list[dict[str, Any]],
    *,
    start_balance: float,
    dynamic: bool,
    step_usd: float,
    step_lot: float,
) -> dict[str, Any]:
    timeline: list[tuple[datetime, int, int, dict[str, Any]]] = []
    for sequence, event in enumerate(events):
        # Entries are processed before exits on the same M1 bar. This avoids using
        # a profit whose exact intrabar close order is unknown to size a new leg.
        timeline.append((event["entry_time"], 0, sequence, event))
        timeline.append((event["exit_time"], 1, sequence, event))
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))

    balance = float(start_balance)
    peak = balance
    max_closed_drawdown = 0.0
    open_trades: dict[int, dict[str, Any]] = {}
    max_concurrent = 0
    max_concurrent_lot = 0.0
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    lot_changes: list[dict[str, Any]] = []
    previous_lot: float | None = None
    closed = 0

    for moment, event_type, sequence, event in timeline:
        if event_type == 0:
            lot = _lot(balance, start_balance, step_usd, step_lot) if dynamic else 0.01
            if previous_lot is None or lot != previous_lot:
                lot_changes.append(
                    {"time": moment.isoformat(), "balance": round(balance, 2), "lot_per_leg": lot}
                )
                previous_lot = lot
            open_trades[sequence] = {**event, "lot": lot}
            max_concurrent = max(max_concurrent, len(open_trades))
            max_concurrent_lot = max(
                max_concurrent_lot,
                sum(float(row["lot"]) for row in open_trades.values()),
            )
            continue

        trade = open_trades.pop(sequence)
        pnl = float(trade["pnl_001"]) * (float(trade["lot"]) / 0.01)
        balance += pnl
        peak = max(peak, balance)
        max_closed_drawdown = min(max_closed_drawdown, balance - peak)
        source = str(trade["source"])
        by_source[source]["positions"] += 1
        by_source[source]["pnl"] += pnl
        if pnl > 0.0:
            by_source[source]["wins"] += 1
        elif pnl < 0.0:
            by_source[source]["losses"] += 1
        closed += 1

    source_rows: dict[str, Any] = {}
    for source, values in sorted(by_source.items()):
        positions = int(values["positions"])
        wins = int(values["wins"])
        losses = int(values["losses"])
        source_rows[source] = {
            "positions": positions,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
            "pnl": round(float(values["pnl"]), 2),
        }

    wins = sum(int(row["wins"]) for row in source_rows.values())
    losses = sum(int(row["losses"]) for row in source_rows.values())
    return {
        "start_balance": round(start_balance, 2),
        "end_balance": round(balance, 2),
        "net_pnl": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "positions": closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "max_closed_drawdown": round(max_closed_drawdown, 2),
        "max_closed_drawdown_pct_of_start": round(100.0 * abs(max_closed_drawdown) / start_balance, 2),
        "max_concurrent_positions": max_concurrent,
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "final_leg_lot": _lot(balance, start_balance, step_usd, step_lot) if dynamic else 0.01,
        "lot_changes": lot_changes,
        "by_source": source_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--step-usd", type=float, default=500.0)
    parser.add_argument("--step-lot", type=float, default=0.01)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument(
        "--output",
        default="data_vantage/phoenix_recommended_dynamic_60sessions_20260810.json",
    )
    args = parser.parse_args()

    full_path = ROOT / "data_vantage/current_phoenix_fast_be_60sessions_20260810.json"
    range_path = ROOT / "data_vantage/current_phoenix_range_tolerance0p5_60sessions_20260810.json"
    audit_path = ROOT / "data_vantage/phoenix_copy_audit_60sessions_20260810.json"
    full_report = _load(full_path)
    range_report = _load(range_path)
    audit = _load(audit_path)

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(ROOT / env_file, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        period_start = _dt(audit["range_utc"]["start"])
        period_end = _dt(audit["range_utc"]["end"])
        rates = _rates(symbol, period_start - timedelta(days=2), period_end + timedelta(hours=1))
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)

        core = _core_events(full_report)
        ranges = _range_events(range_report)
        direction_safe = _direction_events(
            audit=audit,
            rates=rates,
            point=point,
            stop_usd=12.0,
            horizon_minutes=30,
            broker_offset_hours=args.broker_offset_hours,
            commission_per_001=args.commission_per_001,
        )
        direction_runtime = _direction_events(
            audit=audit,
            rates=rates,
            point=point,
            stop_usd=6.0,
            horizon_minutes=15,
            broker_offset_hours=args.broker_offset_hours,
            commission_per_001=args.commission_per_001,
        )

        variants = {
            "recommended_direction_sl12_30m": core + ranges + direction_safe,
            "current_direction_sl6_15m": core + ranges + direction_runtime,
            "core_without_direction": core + ranges,
        }
        output: dict[str, Any] = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": audit["range_utc"],
            "sessions": 60,
            "symbol": symbol,
            "strategy": {
                "full_signal": "TP1 + TP1 + TP2; current fast-BE replay",
                "early_range": "current 15-minute staged pending replay",
                "direction": "pullback-confirmed market TP1 runner",
                "dynamic_lot": (
                    f"0.01 per leg at {args.start_balance:.0f} USD; +{args.step_lot:.2f} per leg "
                    f"for each {args.step_usd:.0f} USD of realized balance growth"
                ),
                "same_bar_order": "entries before exits (conservative sizing)",
            },
            "results": {},
        }
        for name, events in variants.items():
            output["results"][name] = {
                "fixed_001": _simulate(
                    events,
                    start_balance=args.start_balance,
                    dynamic=False,
                    step_usd=args.step_usd,
                    step_lot=args.step_lot,
                ),
                "dynamic": _simulate(
                    events,
                    start_balance=args.start_balance,
                    dynamic=True,
                    step_usd=args.step_usd,
                    step_lot=args.step_lot,
                ),
            }

        output_path = ROOT / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(output_path), **output}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
