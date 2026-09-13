from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_phoenix_complete_60d import _rates
from scripts.backtest_phoenix_recommended_dynamic_60sessions import (
    _core_events,
    _direction_events,
    _dt,
    _load,
    _range_events,
)


WARSAW = ZoneInfo("Europe/Warsaw")


def _floor_lot(value: float) -> float:
    return round(max(0.0, math.floor((float(value) + 1e-9) * 100.0) / 100.0), 2)


def _scalper_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, row in enumerate(report.get("trades", [])):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        spread_per_001 = float(row.get("spread_cost", 0.0) or 0.0) / (original_lot / 0.01)
        events.append(
            {
                "source": "scalper_core",
                "message_id": index + 1,
                "setup_id": str(row["opened"]),
                "entry_time": _dt(row["opened"]),
                "exit_time": _dt(row["closed"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": (
                    float(row["entry"]) - 3.0
                    if str(row["side"]) == "buy"
                    else float(row["entry"]) + 3.0
                ),
                "pnl_001": float(row["profit_001"]) - spread_per_001,
                "status": str(row.get("status", "unknown")),
                "setup_tag": str(row.get("setup_tag", "SC-Core")),
            }
        )
    return events


def _phoenix_lot(balance: float, start_balance: float, base_lot: float, step_usd: float, add_lot: float) -> float:
    steps = math.floor(max(0.0, balance - start_balance + 1e-9) / step_usd)
    return round(base_lot + steps * add_lot, 2)


def _simulate_closed(
    events: list[dict[str, Any]],
    *,
    start_balance: float,
    phoenix_base_lot: float,
    phoenix_step_usd: float,
    phoenix_step_lot: float,
    phoenix_max_open_lot: float,
    scalper_risk_pct: float,
    internal_daily_loss_pct: float,
    internal_total_loss_pct: float,
    ftmo_daily_loss_pct: float,
    ftmo_total_loss_pct: float,
    max_positions: int,
    stop_at_target_pct: float = 0.0,
) -> dict[str, Any]:
    timeline: list[tuple[datetime, int, int, dict[str, Any]]] = []
    for sequence, event in enumerate(events):
        timeline.append((event["entry_time"], 0, sequence, event))
        timeline.append((event["exit_time"], 1, sequence, event))
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))

    scalper_group_sizes: dict[str, int] = defaultdict(int)
    for event in events:
        if event["source"] == "scalper_core":
            scalper_group_sizes[str(event["setup_id"])] += 1

    balance = float(start_balance)
    peak = balance
    max_closed_dd = 0.0
    open_trades: dict[int, dict[str, Any]] = {}
    scalper_lots: dict[str, float] = {}
    accepted: list[dict[str, Any]] = []
    skips: dict[str, int] = defaultdict(int)
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_start_balance: dict[str, float] = {}
    internal_blocked_days: set[str] = set()
    ftmo_daily_breach: dict[str, Any] | None = None
    ftmo_total_breach: dict[str, Any] | None = None
    internal_total_blocked = False
    max_concurrent_positions = 0
    max_concurrent_lot = 0.0
    first_phase_target: str | None = None
    second_phase_target: str | None = None
    phase_target_time: str | None = None
    phase_target_balance: float | None = None
    phase_target_complete = False
    trading_days: set[str] = set()

    for moment, event_type, sequence, event in timeline:
        day = moment.astimezone(WARSAW).date().isoformat()
        daily_start_balance.setdefault(day, balance)
        if event_type == 0:
            if phase_target_complete:
                skips["phase_target_complete"] += 1
                continue
            if internal_total_blocked:
                skips["internal_total_loss_block"] += 1
                continue
            if day in internal_blocked_days:
                skips["internal_daily_loss_block"] += 1
                continue
            if len(open_trades) >= max_positions:
                skips["max_positions"] += 1
                continue

            source = str(event["source"])
            if source.startswith("phoenix"):
                requested_lot = _phoenix_lot(
                    balance,
                    start_balance,
                    phoenix_base_lot,
                    phoenix_step_usd,
                    phoenix_step_lot,
                )
                open_phoenix_lot = sum(
                    float(row["lot"])
                    for row in open_trades.values()
                    if str(row["source"]).startswith("phoenix")
                )
                lot = min(requested_lot, _floor_lot(phoenix_max_open_lot - open_phoenix_lot))
                lot = _floor_lot(lot)
                if lot < 0.01:
                    skips["phoenix_exposure_cap"] += 1
                    continue
            else:
                setup_id = str(event["setup_id"])
                if setup_id not in scalper_lots:
                    setup_risk = balance * scalper_risk_pct / 100.0
                    legs = max(1, scalper_group_sizes[setup_id])
                    # Historical stop plus reconstructed spread is 3.10 USD per 0.01 lot and leg.
                    scalper_lots[setup_id] = max(0.01, _floor_lot(setup_risk * 0.01 / (legs * 3.10)))
                lot = scalper_lots[setup_id]

            trade = {**event, "lot": lot, "sequence": sequence}
            open_trades[sequence] = trade
            accepted.append(trade)
            trading_days.add(day)
            max_concurrent_positions = max(max_concurrent_positions, len(open_trades))
            max_concurrent_lot = max(max_concurrent_lot, sum(float(row["lot"]) for row in open_trades.values()))
            continue

        trade = open_trades.pop(sequence, None)
        if trade is None:
            continue
        pnl = float(trade["pnl_001"]) * (float(trade["lot"]) / 0.01)
        trade["scaled_pnl"] = pnl
        balance += pnl
        peak = max(peak, balance)
        max_closed_dd = min(max_closed_dd, balance - peak)
        daily_pnl[day] += pnl
        source = str(trade["source"])
        by_source[source]["positions"] += 1
        by_source[source]["pnl"] += pnl
        by_source[source]["wins" if pnl > 0.0 else "losses" if pnl < 0.0 else "flat"] += 1

        day_loss = daily_start_balance[day] - balance
        if day_loss >= start_balance * internal_daily_loss_pct / 100.0:
            internal_blocked_days.add(day)
        if balance <= start_balance * (1.0 - internal_total_loss_pct / 100.0):
            internal_total_blocked = True
        if ftmo_daily_breach is None and day_loss >= start_balance * ftmo_daily_loss_pct / 100.0:
            ftmo_daily_breach = {"time": moment.isoformat(), "balance": round(balance, 2), "day": day}
        if ftmo_total_breach is None and balance <= start_balance * (1.0 - ftmo_total_loss_pct / 100.0):
            ftmo_total_breach = {"time": moment.isoformat(), "balance": round(balance, 2)}
        if len(trading_days) >= 4 and not open_trades:
            if second_phase_target is None and balance >= start_balance * 1.05:
                second_phase_target = moment.isoformat()
            if first_phase_target is None and balance >= start_balance * 1.10:
                first_phase_target = moment.isoformat()
            if (
                stop_at_target_pct > 0.0
                and phase_target_time is None
                and balance >= start_balance * (1.0 + stop_at_target_pct / 100.0)
            ):
                phase_target_time = moment.isoformat()
                phase_target_balance = balance
                phase_target_complete = True

    source_rows: dict[str, Any] = {}
    for source, values in sorted(by_source.items()):
        wins = int(values["wins"])
        losses = int(values["losses"])
        source_rows[source] = {
            "positions": int(values["positions"]),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
            "pnl": round(float(values["pnl"]), 2),
        }

    worst_day = min(daily_pnl.items(), key=lambda item: item[1]) if daily_pnl else ("", 0.0)
    best_day = max(daily_pnl.items(), key=lambda item: item[1]) if daily_pnl else ("", 0.0)
    return {
        "start_balance": round(start_balance, 2),
        "end_balance": round(balance, 2),
        "net_pnl": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "max_closed_drawdown": round(max_closed_dd, 2),
        "max_closed_drawdown_pct": round(100.0 * abs(max_closed_dd) / start_balance, 3),
        "positions": sum(int(row["positions"]) for row in source_rows.values()),
        "trading_days": len(trading_days),
        "max_concurrent_positions": max_concurrent_positions,
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "best_day": {"date": best_day[0], "pnl": round(best_day[1], 2)},
        "worst_day": {"date": worst_day[0], "pnl": round(worst_day[1], 2)},
        "internal_daily_blocked_days": sorted(internal_blocked_days),
        "internal_total_blocked": internal_total_blocked,
        "ftmo_daily_breach_closed_balance": ftmo_daily_breach,
        "ftmo_total_breach_closed_balance": ftmo_total_breach,
        "challenge_10pct_target_time": first_phase_target,
        "verification_5pct_target_time": second_phase_target,
        "phase_target_pct": float(stop_at_target_pct),
        "phase_target_time": phase_target_time,
        "phase_target_balance": round(phase_target_balance, 2) if phase_target_balance is not None else None,
        "skips": dict(sorted(skips.items())),
        "by_source": source_rows,
        "daily_pnl": dict(sorted((day, round(value, 2)) for day, value in daily_pnl.items())),
        "accepted_trades": accepted,
    }


def _floating_audit(
    rates: pd.DataFrame,
    trades: list[dict[str, Any]],
    *,
    start_balance: float,
    point: float,
    ftmo_daily_loss_pct: float,
    ftmo_total_loss_pct: float,
    internal_daily_loss_pct: float,
    internal_total_loss_pct: float,
) -> dict[str, Any]:
    entries: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    exits: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        entries[trade["entry_time"]].append(trade)
        exits[trade["exit_time"]].append(trade)

    balance = float(start_balance)
    peak_equity = balance
    max_floating_dd = 0.0
    active: dict[int, dict[str, Any]] = {}
    day_start_balance: dict[str, float] = {}
    day_worst_equity: dict[str, float] = {}
    ftmo_daily_breach: dict[str, Any] | None = None
    ftmo_total_breach: dict[str, Any] | None = None
    internal_daily_touch: dict[str, Any] | None = None
    internal_total_touch: dict[str, Any] | None = None
    missing_entry_prices = 0

    for bar in rates.itertuples(index=False):
        moment = _dt(pd.Timestamp(bar.time).isoformat())
        day = moment.astimezone(WARSAW).date().isoformat()
        day_start_balance.setdefault(day, balance)
        day_worst_equity.setdefault(day, balance)
        for trade in entries.get(moment, []):
            active[int(trade["sequence"])] = trade

        open_pnl = 0.0
        for trade in active.values():
            entry = float(trade.get("entry", 0.0) or 0.0)
            if entry <= 0.0:
                missing_entry_prices += 1
                continue
            spread = float(bar.spread) * point
            if str(trade["side"]) == "buy":
                adverse_exit = float(bar.low)
                move = adverse_exit - entry
            else:
                adverse_exit = float(bar.high) + spread
                move = entry - adverse_exit
            commission = 0.06 * (float(trade["lot"]) / 0.01)
            open_pnl += move * (float(trade["lot"]) / 0.01) - commission

        worst_equity = balance + open_pnl
        day_worst_equity[day] = min(day_worst_equity[day], worst_equity)
        peak_equity = max(peak_equity, worst_equity)
        max_floating_dd = min(max_floating_dd, worst_equity - peak_equity)
        daily_loss = day_start_balance[day] - worst_equity
        total_loss = start_balance - worst_equity
        if internal_daily_touch is None and daily_loss >= start_balance * internal_daily_loss_pct / 100.0:
            internal_daily_touch = {"time": moment.isoformat(), "equity": round(worst_equity, 2)}
        if internal_total_touch is None and total_loss >= start_balance * internal_total_loss_pct / 100.0:
            internal_total_touch = {"time": moment.isoformat(), "equity": round(worst_equity, 2)}
        if ftmo_daily_breach is None and daily_loss >= start_balance * ftmo_daily_loss_pct / 100.0:
            ftmo_daily_breach = {"time": moment.isoformat(), "equity": round(worst_equity, 2)}
        if ftmo_total_breach is None and total_loss >= start_balance * ftmo_total_loss_pct / 100.0:
            ftmo_total_breach = {"time": moment.isoformat(), "equity": round(worst_equity, 2)}

        for trade in exits.get(moment, []):
            balance += float(trade.get("scaled_pnl", 0.0) or 0.0)
            active.pop(int(trade["sequence"]), None)

    worst_day = min(
        day_worst_equity,
        key=lambda day: day_worst_equity[day] - day_start_balance[day],
    )
    return {
        "max_m1_floating_drawdown": round(max_floating_dd, 2),
        "max_m1_floating_drawdown_pct_of_start": round(100.0 * abs(max_floating_dd) / start_balance, 3),
        "worst_intraday": {
            "date": worst_day,
            "drawdown": round(day_worst_equity[worst_day] - day_start_balance[worst_day], 2),
            "drawdown_pct_of_start": round(
                100.0 * (day_worst_equity[worst_day] - day_start_balance[worst_day]) / start_balance,
                3,
            ),
        },
        "internal_daily_limit_touched": internal_daily_touch,
        "internal_total_limit_touched": internal_total_touch,
        "ftmo_daily_limit_breached": ftmo_daily_breach,
        "ftmo_total_limit_breached": ftmo_total_breach,
        "missing_entry_price_observations": missing_entry_prices,
        "method": "conservative M1 adverse high/low mark for every simultaneously open leg; commission included",
    }


def _simulate_m1_guarded(
    rates: pd.DataFrame,
    events: list[dict[str, Any]],
    *,
    start_balance: float,
    point: float,
    phoenix_base_lot: float,
    phoenix_step_lot: float,
    phoenix_max_open_lot: float,
    scalper_risk_pct: float,
    internal_daily_loss_pct: float,
    internal_total_loss_pct: float,
    ftmo_daily_loss_pct: float,
    ftmo_total_loss_pct: float,
    max_positions: int,
    stop_at_target_pct: float = 0.0,
) -> dict[str, Any]:
    entries: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    exits: dict[datetime, list[int]] = defaultdict(list)
    scalper_group_sizes: dict[str, int] = defaultdict(int)
    for sequence, raw in enumerate(events):
        event = {**raw, "sequence": sequence}
        entries[event["entry_time"]].append(event)
        exits[event["exit_time"]].append(sequence)
        if event["source"] == "scalper_core":
            scalper_group_sizes[str(event["setup_id"])] += 1

    balance = float(start_balance)
    peak_balance = balance
    peak_equity = balance
    max_closed_dd = 0.0
    max_floating_dd = 0.0
    active: dict[int, dict[str, Any]] = {}
    scalper_lots: dict[str, float] = {}
    daily_start_balance: dict[str, float] = {}
    daily_pnl: dict[str, float] = defaultdict(float)
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    skips: dict[str, int] = defaultdict(int)
    trading_days: set[str] = set()
    blocked_days: set[str] = set()
    guard_events: list[dict[str, Any]] = []
    total_blocked = False
    target_time: str | None = None
    target_balance: float | None = None
    target_complete = False
    ftmo_daily_breach: dict[str, Any] | None = None
    ftmo_total_breach: dict[str, Any] | None = None
    max_concurrent_positions = 0
    max_concurrent_lot = 0.0

    def record_close(trade: dict[str, Any], pnl: float, day: str, forced: bool) -> None:
        nonlocal balance, peak_balance, max_closed_dd
        balance += pnl
        peak_balance = max(peak_balance, balance)
        max_closed_dd = min(max_closed_dd, balance - peak_balance)
        daily_pnl[day] += pnl
        source = str(trade["source"])
        by_source[source]["positions"] += 1
        by_source[source]["pnl"] += pnl
        by_source[source]["wins" if pnl > 0.0 else "losses" if pnl < 0.0 else "flat"] += 1
        if forced:
            by_source[source]["forced_closes"] += 1

    first_event = min((event["entry_time"] for event in events), default=None)
    last_event = max((event["exit_time"] for event in events), default=None)
    frame = rates
    if first_event is not None and last_event is not None:
        frame = rates[
            (rates["time"] >= pd.Timestamp(first_event) - pd.Timedelta(minutes=1))
            & (rates["time"] <= pd.Timestamp(last_event) + pd.Timedelta(minutes=1))
        ]

    for bar in frame.itertuples(index=False):
        moment = _dt(pd.Timestamp(bar.time).isoformat())
        day = moment.astimezone(WARSAW).date().isoformat()
        daily_start_balance.setdefault(day, balance)

        for event in entries.get(moment, []):
            if target_complete:
                skips["phase_target_complete"] += 1
                continue
            if total_blocked:
                skips["internal_total_loss_block"] += 1
                continue
            if day in blocked_days:
                skips["internal_daily_loss_block"] += 1
                continue
            if len(active) >= max_positions:
                skips["max_positions"] += 1
                continue
            if str(event["source"]).startswith("phoenix"):
                requested = _phoenix_lot(
                    balance,
                    start_balance,
                    phoenix_base_lot,
                    5000.0,
                    phoenix_step_lot,
                )
                used = sum(
                    float(row["lot"])
                    for row in active.values()
                    if str(row["source"]).startswith("phoenix")
                )
                lot = _floor_lot(min(requested, _floor_lot(phoenix_max_open_lot - used)))
                if lot < 0.01:
                    skips["phoenix_exposure_cap"] += 1
                    continue
            else:
                setup_id = str(event["setup_id"])
                if setup_id not in scalper_lots:
                    risk_usd = balance * scalper_risk_pct / 100.0
                    legs = max(1, scalper_group_sizes[setup_id])
                    scalper_lots[setup_id] = max(0.01, _floor_lot(risk_usd * 0.01 / (legs * 3.10)))
                lot = scalper_lots[setup_id]
            active[int(event["sequence"])] = {**event, "lot": lot}
            trading_days.add(day)
            max_concurrent_positions = max(max_concurrent_positions, len(active))
            max_concurrent_lot = max(max_concurrent_lot, sum(float(row["lot"]) for row in active.values()))

        adverse_pnl: dict[int, float] = {}
        for sequence, trade in active.items():
            spread = float(bar.spread) * point
            entry = float(trade["entry"])
            if str(trade["side"]) == "buy":
                move = float(bar.low) - entry
            else:
                move = entry - (float(bar.high) + spread)
            commission = 0.06 * (float(trade["lot"]) / 0.01)
            adverse_pnl[sequence] = move * (float(trade["lot"]) / 0.01) - commission
        worst_equity = balance + sum(adverse_pnl.values())
        peak_equity = max(peak_equity, balance, worst_equity)
        max_floating_dd = min(max_floating_dd, worst_equity - peak_equity)
        daily_loss = daily_start_balance[day] - worst_equity
        total_loss = start_balance - worst_equity
        if ftmo_daily_breach is None and daily_loss >= start_balance * ftmo_daily_loss_pct / 100.0:
            ftmo_daily_breach = {"time": moment.isoformat(), "equity": round(worst_equity, 2)}
        if ftmo_total_breach is None and total_loss >= start_balance * ftmo_total_loss_pct / 100.0:
            ftmo_total_breach = {"time": moment.isoformat(), "equity": round(worst_equity, 2)}

        daily_guard = daily_loss >= start_balance * internal_daily_loss_pct / 100.0
        total_guard = total_loss >= start_balance * internal_total_loss_pct / 100.0
        if active and (daily_guard or total_guard):
            reason = "internal_total_loss" if total_guard else "internal_daily_loss"
            before = balance
            for sequence, trade in list(active.items()):
                record_close(trade, adverse_pnl[sequence], day, True)
            active.clear()
            blocked_days.add(day)
            total_blocked = total_blocked or total_guard
            guard_events.append(
                {
                    "time": moment.isoformat(),
                    "day": day,
                    "reason": reason,
                    "equity_at_trigger": round(worst_equity, 2),
                    "balance_before": round(before, 2),
                    "balance_after": round(balance, 2),
                }
            )
        else:
            for sequence in exits.get(moment, []):
                trade = active.pop(sequence, None)
                if trade is None:
                    continue
                pnl = float(trade["pnl_001"]) * (float(trade["lot"]) / 0.01)
                record_close(trade, pnl, day, False)

        if (
            stop_at_target_pct > 0.0
            and target_time is None
            and len(trading_days) >= 4
            and not active
            and balance >= start_balance * (1.0 + stop_at_target_pct / 100.0)
        ):
            target_time = moment.isoformat()
            target_balance = balance
            target_complete = True

    source_rows: dict[str, Any] = {}
    for source, values in sorted(by_source.items()):
        wins = int(values["wins"])
        losses = int(values["losses"])
        source_rows[source] = {
            "positions": int(values["positions"]),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
            "forced_closes": int(values["forced_closes"]),
            "pnl": round(float(values["pnl"]), 2),
        }
    best_day = max(daily_pnl.items(), key=lambda item: item[1]) if daily_pnl else ("", 0.0)
    worst_day = min(daily_pnl.items(), key=lambda item: item[1]) if daily_pnl else ("", 0.0)
    return {
        "start_balance": round(start_balance, 2),
        "end_balance": round(balance, 2),
        "net_pnl": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "positions": sum(int(row["positions"]) for row in source_rows.values()),
        "trading_days": len(trading_days),
        "max_closed_drawdown": round(max_closed_dd, 2),
        "max_closed_drawdown_pct": round(100.0 * abs(max_closed_dd) / start_balance, 3),
        "max_m1_floating_drawdown": round(max_floating_dd, 2),
        "max_m1_floating_drawdown_pct": round(100.0 * abs(max_floating_dd) / start_balance, 3),
        "max_concurrent_positions": max_concurrent_positions,
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "best_day": {"date": best_day[0], "pnl": round(best_day[1], 2)},
        "worst_day": {"date": worst_day[0], "pnl": round(worst_day[1], 2)},
        "guard_events": guard_events,
        "blocked_days": sorted(blocked_days),
        "target_pct": stop_at_target_pct,
        "target_time": target_time,
        "target_balance": round(target_balance, 2) if target_balance is not None else None,
        "ftmo_daily_limit_breached": ftmo_daily_breach,
        "ftmo_total_limit_breached": ftmo_total_breach,
        "skips": dict(sorted(skips.items())),
        "by_source": source_rows,
        "daily_pnl": dict(sorted((day, round(value, 2)) for day, value in daily_pnl.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data_vantage/ftmo_100k_recommended_60sessions_20260811.json")
    parser.add_argument("--profile-label", default="recommended")
    parser.add_argument("--phoenix-base-lot", type=float, default=0.10)
    parser.add_argument("--phoenix-step-lot", type=float, default=0.01)
    parser.add_argument("--phoenix-max-open-lot", type=float, default=0.50)
    parser.add_argument("--scalper-risk-pct", type=float, default=0.25)
    parser.add_argument("--internal-daily-loss-pct", type=float, default=3.5)
    parser.add_argument("--internal-total-loss-pct", type=float, default=7.5)
    args = parser.parse_args()

    full = _load(ROOT / "data_vantage/current_phoenix_fast_be_60sessions_20260810.json")
    ranges_report = _load(ROOT / "data_vantage/current_phoenix_range_tolerance0p5_60sessions_20260810.json")
    audit = _load(ROOT / "data_vantage/phoenix_copy_audit_60sessions_20260810.json")
    scalper = _load(ROOT / "data_vantage/current_core_scalper_60sessions_start1000_20260810.json")
    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(ROOT / env_file, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        start = _dt(audit["range_utc"]["start"])
        end = _dt(audit["range_utc"]["end"])
        rates = _rates(symbol, start - timedelta(days=2), end + timedelta(hours=1))
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        phoenix = _core_events(full) + _range_events(ranges_report) + _direction_events(
            audit=audit,
            rates=rates,
            point=point,
            stop_usd=12.0,
            horizon_minutes=30,
            broker_offset_hours=3.0,
            commission_per_001=0.06,
        )
        events = phoenix + _scalper_events(scalper)
        result = _simulate_closed(
            events,
            start_balance=100000.0,
            phoenix_base_lot=args.phoenix_base_lot,
            phoenix_step_usd=5000.0,
            phoenix_step_lot=args.phoenix_step_lot,
            phoenix_max_open_lot=args.phoenix_max_open_lot,
            scalper_risk_pct=args.scalper_risk_pct,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            max_positions=8,
        )
        floating = _floating_audit(
            rates,
            result["accepted_trades"],
            start_balance=100000.0,
            point=point,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
        )
        challenge = _simulate_closed(
            events,
            start_balance=100000.0,
            phoenix_base_lot=args.phoenix_base_lot,
            phoenix_step_usd=5000.0,
            phoenix_step_lot=args.phoenix_step_lot,
            phoenix_max_open_lot=args.phoenix_max_open_lot,
            scalper_risk_pct=args.scalper_risk_pct,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            max_positions=8,
            stop_at_target_pct=10.0,
        )
        challenge_target_time = challenge.get("phase_target_time")
        verification_events: list[dict[str, Any]] = []
        if challenge_target_time:
            challenge_day = _dt(str(challenge_target_time)).astimezone(WARSAW).date()
            verification_events = [
                event
                for event in events
                if event["entry_time"].astimezone(WARSAW).date() > challenge_day
            ]
        verification = _simulate_closed(
            verification_events,
            start_balance=100000.0,
            phoenix_base_lot=args.phoenix_base_lot,
            phoenix_step_usd=5000.0,
            phoenix_step_lot=args.phoenix_step_lot,
            phoenix_max_open_lot=args.phoenix_max_open_lot,
            scalper_risk_pct=args.scalper_risk_pct,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            max_positions=8,
            stop_at_target_pct=5.0,
        )
        challenge_trades = challenge.pop("accepted_trades", [])
        verification_trades = verification.pop("accepted_trades", [])
        challenge_floating = _floating_audit(
            rates,
            challenge_trades,
            start_balance=100000.0,
            point=point,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
        )
        verification_floating = _floating_audit(
            rates,
            verification_trades,
            start_balance=100000.0,
            point=point,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
        ) if verification_trades else None
        guarded_full = _simulate_m1_guarded(
            rates,
            events,
            start_balance=100000.0,
            point=point,
            phoenix_base_lot=args.phoenix_base_lot,
            phoenix_step_lot=args.phoenix_step_lot,
            phoenix_max_open_lot=args.phoenix_max_open_lot,
            scalper_risk_pct=args.scalper_risk_pct,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            max_positions=8,
        )
        guarded_challenge = _simulate_m1_guarded(
            rates,
            events,
            start_balance=100000.0,
            point=point,
            phoenix_base_lot=args.phoenix_base_lot,
            phoenix_step_lot=args.phoenix_step_lot,
            phoenix_max_open_lot=args.phoenix_max_open_lot,
            scalper_risk_pct=args.scalper_risk_pct,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            max_positions=8,
            stop_at_target_pct=10.0,
        )
        guarded_verification_events: list[dict[str, Any]] = []
        if guarded_challenge.get("target_time"):
            guarded_challenge_day = _dt(str(guarded_challenge["target_time"])).astimezone(WARSAW).date()
            guarded_verification_events = [
                event
                for event in events
                if event["entry_time"].astimezone(WARSAW).date() > guarded_challenge_day
            ]
        guarded_verification = _simulate_m1_guarded(
            rates,
            guarded_verification_events,
            start_balance=100000.0,
            point=point,
            phoenix_base_lot=args.phoenix_base_lot,
            phoenix_step_lot=args.phoenix_step_lot,
            phoenix_max_open_lot=args.phoenix_max_open_lot,
            scalper_risk_pct=args.scalper_risk_pct,
            internal_daily_loss_pct=args.internal_daily_loss_pct,
            internal_total_loss_pct=args.internal_total_loss_pct,
            ftmo_daily_loss_pct=5.0,
            ftmo_total_loss_pct=10.0,
            max_positions=8,
            stop_at_target_pct=5.0,
        )
        result.pop("accepted_trades", None)
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": audit["range_utc"],
            "sessions": 60,
            "account": {
                "programme": "FTMO Challenge 2-Step Swing",
                "start_balance": 100000.0,
                "phase_1_target": 110000.0,
                "phase_2_target": 105000.0,
                "official_daily_loss_pct": 5.0,
                "official_total_loss_pct": 10.0,
                "internal_daily_loss_pct": args.internal_daily_loss_pct,
                "internal_total_loss_pct": args.internal_total_loss_pct,
            },
            "strategy": {
                "profile_label": args.profile_label,
                "phoenix": "TP1 + TP1 + TP2, fast BE, 15m range pending, pullback direction TP1 SL12/30m",
                "phoenix_lot": (
                    f"{args.phoenix_base_lot:.2f} per leg; +{args.phoenix_step_lot:.2f} for each 5000 USD "
                    f"realized profit; max {args.phoenix_max_open_lot:.2f} open Phoenix lot"
                ),
                "scalper": f"one core engine, {args.scalper_risk_pct:.2f}% setup risk split over three legs",
                "disabled": ["agent teams", "extra indicator packs", "martingale", "long-term modules", "extra deep runners"],
                "max_positions": 8,
            },
            "closed_replay": result,
            "floating_m1_audit": floating,
            "guarded_m1_replay": guarded_full,
            "sequential_evaluation": {
                "assumption": "Verification starts on the next trading day after Challenge completion; account resets to 100000 USD.",
                "challenge_10pct": {"closed_replay": challenge, "floating_m1_audit": challenge_floating},
                "verification_5pct": {"closed_replay": verification, "floating_m1_audit": verification_floating},
            },
            "sequential_evaluation_with_internal_equity_guard": {
                "challenge_10pct": guarded_challenge,
                "verification_5pct": guarded_verification,
            },
            "limitations": [
                "M1 adverse high/low is conservative but cannot reconstruct tick order or slippage exactly.",
                "FTMO spreads can differ from the Vantage historical feed used by the source reports.",
                "This is an in-sample historical reconstruction, not a guarantee of passing or future profit.",
            ],
        }
        path = ROOT / args.output
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(path), **output}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
