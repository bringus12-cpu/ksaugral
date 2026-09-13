from __future__ import annotations

import argparse
import bisect
import json
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * pct
    low = int(index)
    high = min(len(ordered) - 1, low + 1)
    weight = index - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _stochastic_replay(
    trades: list[dict[str, Any]],
    *,
    start_balance: float,
    runs: int,
    miss_probability: float,
    execution_error_probability: float,
    execution_drag_r: float,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    finals: list[float] = []
    drawdowns: list[float] = []
    ruined = 0
    for _ in range(runs):
        balance = float(start_balance)
        peak = balance
        max_dd_pct = 0.0
        for row in trades:
            if balance <= 0.0:
                ruined += 1
                balance = 0.0
                break
            if rng.random() < miss_probability:
                continue
            baseline = max(0.01, float(row.get("balance_before", start_balance) or start_balance))
            scale = balance / baseline
            initial_risk = max(0.0, float(row.get("initial_risk", 0.0) or 0.0)) * scale
            pnl = float(row.get("pnl", 0.0) or 0.0) * scale
            if rng.random() < execution_error_probability:
                pnl = -initial_risk
            pnl -= execution_drag_r * initial_risk
            balance += pnl
            peak = max(peak, balance)
            max_dd_pct = max(max_dd_pct, 100.0 * (peak - balance) / max(0.01, peak))
        finals.append(max(0.0, balance))
        drawdowns.append(max_dd_pct)
    return {
        "runs": runs,
        "miss_probability_pct": round(100.0 * miss_probability, 2),
        "execution_error_probability_pct": round(100.0 * execution_error_probability, 2),
        "execution_drag_r_per_filled_trade": execution_drag_r,
        "final_balance": {
            "p10": round(_percentile(finals, 0.10), 2),
            "median": round(median(finals), 2),
            "p90": round(_percentile(finals, 0.90), 2),
        },
        "max_closed_drawdown_pct": {
            "p10": round(_percentile(drawdowns, 0.10), 2),
            "median": round(median(drawdowns), 2),
            "p90": round(_percentile(drawdowns, 0.90), 2),
        },
        "ruin_probability_pct": round(100.0 * ruined / max(1, runs), 2),
        "finish_below_start_probability_pct": round(
            100.0 * sum(value < start_balance for value in finals) / max(1, runs), 2
        ),
        "finish_below_half_start_probability_pct": round(
            100.0 * sum(value < start_balance * 0.5 for value in finals) / max(1, runs), 2
        ),
    }


def _future_bootstrap(
    trades: list[dict[str, Any]],
    *,
    start_balance: float,
    future_sessions: int,
    risk_pct_per_leg: float,
    runs: int,
    miss_probability: float,
    execution_error_probability: float,
    execution_drag_r: float,
    seed: int,
) -> dict[str, Any]:
    days: dict[str, list[float]] = defaultdict(list)
    for row in trades:
        initial_risk = float(row.get("initial_risk", 0.0) or 0.0)
        if initial_risk <= 0.0:
            continue
        days[_dt(row["closed"]).date().isoformat()].append(float(row.get("pnl", 0.0) or 0.0) / initial_risk)
    blocks = list(days.values())
    rng = random.Random(seed)
    finals: list[float] = []
    drawdowns: list[float] = []
    ruined = 0
    for _ in range(runs):
        balance = float(start_balance)
        peak = balance
        max_dd_pct = 0.0
        for _session in range(future_sessions):
            for return_r in rng.choice(blocks):
                if rng.random() < miss_probability:
                    continue
                risk = balance * risk_pct_per_leg / 100.0
                realized_r = -1.0 if rng.random() < execution_error_probability else return_r
                balance += risk * (realized_r - execution_drag_r)
                peak = max(peak, balance)
                max_dd_pct = max(max_dd_pct, 100.0 * (peak - balance) / max(0.01, peak))
                if balance <= 0.0:
                    break
            if balance <= 0.0:
                ruined += 1
                balance = 0.0
                break
        finals.append(max(0.0, balance))
        drawdowns.append(max_dd_pct)
    return {
        "runs": runs,
        "future_sessions": future_sessions,
        "sampled_historical_day_blocks": len(blocks),
        "final_balance": {
            "p10": round(_percentile(finals, 0.10), 2),
            "median": round(median(finals), 2),
            "p90": round(_percentile(finals, 0.90), 2),
        },
        "max_closed_drawdown_pct": {
            "p10": round(_percentile(drawdowns, 0.10), 2),
            "median": round(median(drawdowns), 2),
            "p90": round(_percentile(drawdowns, 0.90), 2),
        },
        "ruin_probability_pct": round(100.0 * ruined / max(1, runs), 2),
        "finish_below_start_probability_pct": round(
            100.0 * sum(value < start_balance for value in finals) / max(1, runs), 2
        ),
        "finish_below_half_start_probability_pct": round(
            100.0 * sum(value < start_balance * 0.5 for value in finals) / max(1, runs), 2
        ),
    }


def _concurrent_risk_audit(trades: list[dict[str, Any]]) -> dict[str, Any]:
    events: dict[datetime, list[tuple[int, int, dict[str, Any]]]] = defaultdict(list)
    for sequence, row in enumerate(trades):
        events[_dt(row["opened"])].append((1, sequence, row))
        events[_dt(row["closed"])].append((-1, sequence, row))
    active: dict[int, dict[str, Any]] = {}
    worst: dict[str, Any] = {}
    for moment in sorted(events):
        batch = events[moment]
        for kind, sequence, _row in batch:
            if kind < 0 and sequence in active:
                active.pop(sequence, None)
        opened_now = []
        for kind, sequence, row in batch:
            if kind > 0:
                active[sequence] = row
                opened_now.append((sequence, row))
        if not opened_now:
            continue
        row = opened_now[-1][1]
        risk = sum(float(item.get("initial_risk", 0.0) or 0.0) for item in active.values())
        margin = sum(float(item.get("margin", 0.0) or 0.0) for item in active.values())
        balance = float(row.get("balance_before", 0.0) or 0.0)
        risk_pct = 100.0 * risk / max(0.01, balance)
        post_stop_equity = balance - risk
        margin_level = 100.0 * post_stop_equity / margin if margin > 0 else 999999.0
        if risk_pct > float(worst.get("combined_initial_risk_pct", -1.0)):
            worst = {
                "time": moment.isoformat(),
                "open_positions": len(active),
                "balance": round(balance, 2),
                "combined_initial_risk": round(risk, 2),
                "combined_initial_risk_pct": round(risk_pct, 2),
                "used_margin": round(margin, 2),
                "equity_if_all_stops_hit": round(post_stop_equity, 2),
                "margin_level_if_all_stops_hit_pct": round(margin_level, 2),
            }
        for sequence, opened_row in opened_now:
            if _dt(opened_row["closed"]) == moment:
                active.pop(sequence, None)
    return worst


def _m1_floating_audit(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {}
    start = min(_dt(row["opened"]) for row in trades) - timedelta(minutes=1)
    end = max(_dt(row["closed"]) for row in trades) + timedelta(minutes=1)
    rate_maps: dict[str, dict[int, Any]] = {}
    for raw_symbol in sorted({str(row["symbol"]) for row in trades}):
        symbol = ensure_symbol(raw_symbol)
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
        rate_maps[raw_symbol] = {int(bar["time"]): bar for bar in rates} if rates is not None else {}

    opens: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    closes: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for sequence, row in enumerate(trades):
        opens[int(_dt(row["opened"]).timestamp()) // 60 * 60].append((sequence, row))
        closes[int(_dt(row["closed"]).timestamp()) // 60 * 60].append((sequence, row))

    first_minute = min(opens)
    last_minute = max(closes)
    active: dict[int, dict[str, Any]] = {}
    balance = float(trades[0].get("balance_before", 0.0) or 0.0)
    peak_equity = balance
    minimum_equity = balance
    max_dd_pct = 0.0
    minimum_margin_level = 999999.0
    worst_time = first_minute
    missing_bars = 0
    active_position_bar_observations = 0
    for minute in range(first_minute, last_minute + 60, 60):
        for sequence, row in opens.get(minute, []):
            active[sequence] = row
        floating = 0.0
        used_margin = 0.0
        for row in active.values():
            active_position_bar_observations += 1
            bar = rate_maps.get(str(row["symbol"]), {}).get(minute)
            if bar is None:
                missing_bars += 1
                continue
            entry = float(row.get("entry", 0.0) or 0.0)
            sl = float(row.get("sl", 0.0) or 0.0)
            lot = float(row.get("lot", 0.0) or 0.0)
            if row["side"] == "buy":
                adverse = float(bar["low"])
                if sl > 0.0 and sl < entry:
                    adverse = max(adverse, sl)
                order_type = mt5.ORDER_TYPE_BUY
            else:
                adverse = float(bar["high"])
                if sl > entry:
                    adverse = min(adverse, sl)
                order_type = mt5.ORDER_TYPE_SELL
            profit = mt5.order_calc_profit(order_type, str(row["symbol"]), lot, entry, adverse)
            floating += float(profit or 0.0)
            used_margin += float(row.get("margin", 0.0) or 0.0)
        equity = balance + floating
        peak_equity = max(peak_equity, equity)
        if equity < minimum_equity:
            minimum_equity = equity
            worst_time = minute
        max_dd_pct = max(max_dd_pct, 100.0 * (peak_equity - equity) / max(0.01, peak_equity))
        if used_margin > 0.0:
            minimum_margin_level = min(minimum_margin_level, 100.0 * equity / used_margin)
        for sequence, row in closes.get(minute, []):
            balance += float(row.get("pnl", 0.0) or 0.0)
            active.pop(sequence, None)
    return {
        "method": "M1 adverse high/low for every simultaneously open position, capped at original SL",
        "minimum_equity": round(minimum_equity, 2),
        "worst_time": datetime.fromtimestamp(worst_time, UTC).isoformat(),
        "max_floating_drawdown_pct_from_equity_peak": round(max_dd_pct, 2),
        "minimum_margin_level_pct": round(minimum_margin_level, 2),
        "missing_active_position_bars": missing_bars,
        "active_position_bar_observations": active_position_bar_observations,
        "bar_coverage_pct": round(
            100.0 * (active_position_bar_observations - missing_bars) / max(1, active_position_bar_observations),
            2,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--start-balance", type=float, default=700.0)
    parser.add_argument("--future-sessions", type=int, default=79)
    parser.add_argument("--runs", type=int, default=5000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(ROOT / args.env, override=True)
    cfg = load_settings()
    payload = json.loads((ROOT / args.report).read_text(encoding="utf-8"))
    baseline = payload["realistic_with_broker_min_lot_and_margin"]
    trades = baseline["trade_sequence"]

    scenarios = {
        "ideal_delivery": (0.0, 0.0, 0.0),
        "realistic_execution": (0.05, 0.01, 0.03),
        "stressed_execution": (0.10, 0.03, 0.07),
        "severe_execution": (0.20, 0.07, 0.12),
    }
    historical = {
        name: _stochastic_replay(
            trades,
            start_balance=args.start_balance,
            runs=args.runs,
            miss_probability=miss,
            execution_error_probability=error,
            execution_drag_r=drag,
            seed=1100 + offset,
        )
        for offset, (name, (miss, error, drag)) in enumerate(scenarios.items())
    }
    future = {
        name: _future_bootstrap(
            trades,
            start_balance=args.start_balance,
            future_sessions=args.future_sessions,
            risk_pct_per_leg=1.75,
            runs=args.runs,
            miss_probability=miss,
            execution_error_probability=error,
            execution_drag_r=drag,
            seed=2100 + offset,
        )
        for offset, (name, (miss, error, drag)) in enumerate(scenarios.items())
    }

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        floating = _m1_floating_audit(trades)
        account = mt5.account_info()
        stopout = {
            "broker_margin_call_level_pct": float(getattr(account, "margin_so_call", 0.0) or 0.0),
            "broker_stop_out_level_pct": float(getattr(account, "margin_so_so", 0.0) or 0.0),
        }
    finally:
        shutdown()

    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source_report": args.report,
        "baseline": {
            key: baseline[key]
            for key in (
                "start_balance",
                "final_balance",
                "profit",
                "max_closed_drawdown_pct_peak",
                "max_concurrent_positions",
                "skipped_for_margin",
            )
        },
        "concurrent_initial_risk": _concurrent_risk_audit(trades),
        "m1_floating_equity": floating,
        "broker_stop_out": stopout,
        "historical_execution_stress": historical,
        "future_session_bootstrap": future,
        "limitations": [
            "M1 high/low is deliberately adverse and cannot reconstruct tick order inside a minute.",
            "Telegram history exposes final edited text, not every historical edit version.",
            "Execution drag is expressed as a fraction of initial R because instruments have different tick values.",
            "Future bootstrap resamples historical day blocks and is not a promise of future performance.",
        ],
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
