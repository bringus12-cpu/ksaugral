from __future__ import annotations

import argparse
import heapq
import json
import random
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _loss_per_lot(symbol: str, side: str, entry: float, sl: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return abs(float(mt5.order_calc_profit(order_type, symbol, 1.0, entry, sl) or 0.0))


def _normal(symbol: str, cfg: Any, value: float) -> float:
    return normalize_volume(symbol, value, float(cfg.min_lot), max(999.0, float(cfg.max_lot)))


def _covered_signal_ids(listener: dict[str, Any], ranges: dict[str, Any]) -> set[int]:
    candidates: dict[int, dict[str, Any]] = {}
    for row in ranges.get("current_live_three_leg_events", []):
        candidates[int(row["range_message_id"])] = {
            "time": _dt(row["signal_time"]),
            "side": str(row["side"]),
        }
    covered: set[int] = set()
    for row in listener.get("equity", []):
        signal_time = _dt(row["signal_time"])
        for candidate in candidates.values():
            delay = signal_time - candidate["time"]
            if str(row["side"]) == candidate["side"] and timedelta(0) <= delay <= timedelta(minutes=5):
                covered.add(int(row["message_id"]))
                break
    return covered


def _listener_events(listener: dict[str, Any], ranges: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    covered = _covered_signal_ids(listener, ranges)
    events: list[dict[str, Any]] = []
    removed = 0
    for row in listener.get("equity", []):
        message_id = int(row["message_id"])
        if message_id in covered:
            removed += 1
            continue
        plan_index = int(row.get("plan_index", 0))
        events.append(
            {
                "source": "phoenix_tp1_runner" if plan_index == 999 else "phoenix_full",
                "setup": f"phoenix:{message_id}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["initial_sl"]),
                "profit_001": float(row["profit_001"]),
                "spread_per_001": float(row.get("spread_cost", 0.0)),
                "plan_index": plan_index,
            }
        )
    return events, {
        "covered_full_signal_messages": len(covered),
        "listener_legs_removed_as_preliminary_duplicates": removed,
    }


def _range_events(ranges: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in ranges.get("current_live_three_leg_events", []):
        side = str(row["side"])
        entry = float(row["entry"])
        events.append(
            {
                "source": "phoenix_range",
                "setup": f"range:{int(row['range_message_id'])}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": side,
                "entry": entry,
                "sl": entry - 6.0 if side == "buy" else entry + 6.0,
                "pnl_1lot_net": float(row["pnl"]),
                "plan_index": int(row["leg_index"]),
            }
        )
    return events


def _direction_events(direction: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in direction.get("trades", []):
        if str(row.get("source", "")) != "direction_3leg":
            continue
        lot = max(0.01, float(row.get("lot", 0.10)))
        events.append(
            {
                "source": "phoenix_direction",
                "setup": f"direction:{int(row['message_id'])}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["sl"]),
                "pnl_1lot_net": float(row["pnl"]) / lot,
                "plan_index": 0,
            }
        )
    return events


def _extra_tp6_runner_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    spread_per_001 = float(report.get("spread_cost_per_leg", 0.0) or 0.0)
    for row in report.get("selected_extra_tp6_runner_events", []):
        events.append(
            {
                "source": "phoenix_tp6_runner",
                "setup": f"phoenix:{int(row['message_id'])}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["initial_sl"]),
                "profit_001": float(row["pnl"]),
                "spread_per_001": spread_per_001,
                "plan_index": 998,
            }
        )
    return events


def _scalper_events(label: str, scalper: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    source = f"scalper_{label}"
    for index, row in enumerate(scalper.get("trades", [])):
        side = str(row["side"])
        entry = float(row["entry"])
        original_lot = max(0.01, float(row.get("leg_lot", 0.01)))
        opened = _dt(row["opened"])
        events.append(
            {
                "source": source,
                "setup": f"{source}:{opened.isoformat()}",
                "opened": opened,
                "closed": _dt(row["closed"]),
                "side": side,
                "entry": entry,
                "sl": entry - 3.0 if side == "buy" else entry + 3.0,
                "profit_001": float(row["profit_001"]),
                "spread_per_001": float(row.get("spread_cost", 0.0)) / (original_lot / 0.01),
                "plan_index": index,
            }
        )
    return events


def _weekday_dates(start: datetime, end: datetime) -> list[date]:
    cursor = start.date()
    output: list[date] = []
    while cursor <= end.date():
        if cursor.weekday() < 5:
            output.append(cursor)
        cursor += timedelta(days=1)
    return output


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = int(position)
    high = min(len(ordered) - 1, low + 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _simulate(
    *,
    events: list[dict[str, Any]],
    symbol: str,
    cfg: Any,
    start_balance: float,
    risk_pct: float,
    minimum_leg_lot: float,
    runner_multiplier: float,
    enforce_margin: bool,
) -> dict[str, Any]:
    setup_counts: dict[str, int] = defaultdict(int)
    for event in events:
        setup_counts[event["setup"]] += 1

    balance = float(start_balance)
    peak = balance
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    used_margin = 0.0
    max_used_margin = 0.0
    max_concurrent = 0
    max_concurrent_lot = 0.0
    accepted = 0
    margin_skips = 0
    first_margin_skip: dict[str, Any] | None = None
    first_nonpositive_balance: str | None = None
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
    sequence = 0
    by_source_pnl: dict[str, float] = defaultdict(float)
    by_source_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    daily_pnl: dict[str, float] = defaultdict(float)
    lot_ranges: dict[str, list[float]] = defaultdict(list)
    closed_rows: list[dict[str, Any]] = []

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, max_drawdown, max_drawdown_pct, used_margin, first_nonpositive_balance
        while open_heap and open_heap[0][0] <= moment:
            _closed, _seq, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - float(trade["margin"]))
            pnl = float(trade["pnl"])
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = min(max_drawdown, balance - peak)
            if peak > 0:
                max_drawdown_pct = max(max_drawdown_pct, (peak - balance) / peak * 100.0)
            if balance <= 0.0 and first_nonpositive_balance is None:
                first_nonpositive_balance = trade["closed"].isoformat()
            source = str(trade["source"])
            by_source_pnl[source] += pnl
            daily_pnl[trade["closed"].date().isoformat()] += pnl
            outcome = "wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"
            by_source_counts[source][outcome] += 1
            closed_rows.append(
                {
                    "closed": trade["closed"].isoformat(),
                    "source": source,
                    "setup": trade["setup"],
                    "lot": round(float(trade["lot"]), 2),
                    "pnl": round(pnl, 2),
                    "balance": round(balance, 2),
                }
            )

    for event in events:
        close_until(event["opened"])
        source = str(event["source"])
        count = max(1, setup_counts[event["setup"]])
        loss_per_lot = _loss_per_lot(symbol, event["side"], event["entry"], event["sl"])
        risk_per_leg = max(0.0, balance) * risk_pct / 100.0 / count
        ordinary_lot = _normal(
            symbol,
            cfg,
            max(minimum_leg_lot, risk_per_leg / loss_per_lot if loss_per_lot > 0.0 else minimum_leg_lot),
        )
        lot = (
            _normal(symbol, cfg, ordinary_lot * runner_multiplier)
            if source == "phoenix_tp1_runner"
            else ordinary_lot
        )
        margin = _margin(symbol, event["side"], lot, event["entry"])
        free_margin = balance - used_margin
        if balance <= 0.0 or (enforce_margin and margin > free_margin + 0.01):
            margin_skips += 1
            if first_margin_skip is None:
                first_margin_skip = {
                    "time": event["opened"].isoformat(),
                    "source": source,
                    "balance": round(balance, 2),
                    "free_margin": round(free_margin, 2),
                    "required_margin": round(margin, 2),
                    "lot": round(lot, 2),
                }
            continue

        if "pnl_1lot_net" in event:
            pnl = float(event["pnl_1lot_net"]) * lot
        else:
            scale = lot / 0.01
            pnl = (float(event["profit_001"]) - float(event["spread_per_001"])) * scale
        trade = {**event, "lot": lot, "margin": margin, "pnl": pnl}
        sequence += 1
        heapq.heappush(open_heap, (trade["closed"], sequence, trade))
        used_margin += margin
        accepted += 1
        lot_ranges[source].append(lot)
        max_used_margin = max(max_used_margin, used_margin)
        max_concurrent = max(max_concurrent, len(open_heap))
        max_concurrent_lot = max(max_concurrent_lot, sum(float(item[2]["lot"]) for item in open_heap))

    close_until(datetime.max.replace(tzinfo=UTC))
    source_summary: dict[str, Any] = {}
    for source in sorted(set(by_source_pnl) | set(by_source_counts)):
        counts = by_source_counts[source]
        decided = counts["wins"] + counts["losses"]
        lots = lot_ranges[source]
        source_summary[source] = {
            "pnl": round(by_source_pnl[source], 2),
            "wins": counts["wins"],
            "losses": counts["losses"],
            "flat": counts["flat"],
            "win_rate_pct": round(100.0 * counts["wins"] / max(1, decided), 2),
            "min_lot": round(min(lots), 2) if lots else 0.0,
            "max_lot": round(max(lots), 2) if lots else 0.0,
        }
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
        "max_closed_drawdown_usd": round(max_drawdown, 2),
        "max_closed_drawdown_pct_of_peak": round(max_drawdown_pct, 2),
        "first_nonpositive_balance": first_nonpositive_balance,
        "candidate_positions": len(events),
        "accepted_positions": accepted,
        "skipped_for_balance_or_margin": margin_skips,
        "first_margin_skip": first_margin_skip,
        "max_concurrent_positions": max_concurrent,
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "max_used_margin": round(max_used_margin, 2),
        "by_source": source_summary,
        "daily_pnl": {key: round(value, 2) for key, value in sorted(daily_pnl.items())},
        "closed_trades": closed_rows,
    }


def _daily_returns(result: dict[str, Any], session_dates: list[date]) -> list[float]:
    balance = float(result["start_balance"])
    pnl = result["daily_pnl"]
    returns: list[float] = []
    for session_date in session_dates:
        value = float(pnl.get(session_date.isoformat(), 0.0))
        returns.append(value / balance if balance > 0.0 else -1.0)
        balance += value
    return returns


def _bootstrap_forecast(
    daily_returns: list[float],
    *,
    start_balance: float,
    sessions: int,
    trials: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    finals: list[float] = []
    ruined = 0
    for _ in range(trials):
        balance = start_balance
        for _session in range(sessions):
            balance *= 1.0 + rng.choice(daily_returns)
            if balance <= 0.0:
                balance = 0.0
                ruined += 1
                break
        finals.append(balance)
    return {
        "start_balance": round(start_balance, 2),
        "sessions": sessions,
        "trials": trials,
        "median_final_balance": round(_percentile(finals, 0.50), 2),
        "p10_final_balance": round(_percentile(finals, 0.10), 2),
        "p90_final_balance": round(_percentile(finals, 0.90), 2),
        "probability_of_loss_pct": round(100.0 * sum(value < start_balance for value in finals) / trials, 2),
        "probability_of_ruin_pct": round(100.0 * ruined / trials, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--listener", required=True)
    parser.add_argument("--ranges", required=True)
    parser.add_argument("--direction", default="")
    parser.add_argument("--extra-runner", default="")
    parser.add_argument("--scalper", action="append", default=[], help="LABEL=JSON")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=3.0)
    parser.add_argument("--minimum-leg-lot", type=float, default=0.10)
    parser.add_argument("--runner-multiplier", type=float, default=10.0)
    parser.add_argument("--forecast-sessions", type=int, default=60)
    parser.add_argument("--forecast-trials", type=int, default=10000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        listener = _load(args.listener)
        ranges = _load(args.ranges)
        listener_events, reconciliation = _listener_events(listener, ranges)
        events = listener_events + _range_events(ranges)
        if args.direction:
            events.extend(_direction_events(_load(args.direction)))
        if args.extra_runner:
            events.extend(_extra_tp6_runner_events(_load(args.extra_runner)))
        scalper_inputs: dict[str, str] = {}
        for item in args.scalper:
            label, path = item.split("=", 1)
            scalper_inputs[label] = path
            events.extend(_scalper_events(label, _load(path)))

        start = _dt(args.start)
        end = _dt(args.end)
        events = [event for event in events if start <= event["opened"] <= end]
        events.sort(key=lambda row: (row["opened"], row["setup"], row["plan_index"]))
        session_dates = _weekday_dates(start, end)
        if len(session_dates) > 60:
            session_dates = session_dates[-60:]

        realistic = _simulate(
            events=events,
            symbol=symbol,
            cfg=cfg,
            start_balance=args.start_balance,
            risk_pct=args.risk_pct,
            minimum_leg_lot=args.minimum_leg_lot,
            runner_multiplier=args.runner_multiplier,
            enforce_margin=True,
        )
        theoretical = _simulate(
            events=events,
            symbol=symbol,
            cfg=cfg,
            start_balance=args.start_balance,
            risk_pct=args.risk_pct,
            minimum_leg_lot=args.minimum_leg_lot,
            runner_multiplier=args.runner_multiplier,
            enforce_margin=False,
        )
        repeat_from_1000 = _simulate(
            events=events,
            symbol=symbol,
            cfg=cfg,
            start_balance=args.start_balance,
            risk_pct=args.risk_pct,
            minimum_leg_lot=args.minimum_leg_lot,
            runner_multiplier=args.runner_multiplier,
            enforce_margin=True,
        )
        continuation_start = max(0.01, float(realistic["final_balance"]))
        repeat_from_historical_final = _simulate(
            events=events,
            symbol=symbol,
            cfg=cfg,
            start_balance=continuation_start,
            risk_pct=args.risk_pct,
            minimum_leg_lot=args.minimum_leg_lot,
            runner_multiplier=args.runner_multiplier,
            enforce_margin=True,
        )
        returns = _daily_returns(realistic, session_dates)
        forecast = _bootstrap_forecast(
            returns,
            start_balance=args.start_balance,
            sessions=args.forecast_sessions,
            trials=args.forecast_trials,
            seed=20260727,
        )
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": start.isoformat(), "end": end.isoformat()},
            "trading_sessions": len(session_dates),
            "symbol": symbol,
            "sizing": {
                "minimum_lot_per_position": args.minimum_leg_lot,
                "risk_pct_per_setup": args.risk_pct,
                "rule": "max(minimum leg lot, current closed balance risk split over all setup positions)",
                "phoenix_tp1_runner_multiplier": args.runner_multiplier,
                "artificial_position_limit": None,
                "broker_margin_enforced_in_realistic_result": True,
            },
            "inputs": {
                "listener": args.listener,
                "ranges": args.ranges,
                "direction": args.direction or None,
                "extra_runner": args.extra_runner or None,
                "scalpers": scalper_inputs,
            },
            "reconciliation": reconciliation,
            "historical_realistic": realistic,
            "historical_theoretical_without_margin_limit": theoretical,
            "next_60_sessions": {
                "deterministic_repeat_from_same_start": {
                    key: repeat_from_1000[key]
                    for key in (
                        "start_balance",
                        "final_balance",
                        "profit",
                        "return_pct",
                        "max_closed_drawdown_usd",
                        "max_closed_drawdown_pct_of_peak",
                    )
                },
                "deterministic_repeat_continuing_from_historical_final": {
                    key: repeat_from_historical_final[key]
                    for key in (
                        "start_balance",
                        "final_balance",
                        "profit",
                        "return_pct",
                        "max_closed_drawdown_usd",
                        "max_closed_drawdown_pct_of_peak",
                    )
                },
                "bootstrap_from_same_start": forecast,
            },
            "limitations": [
                "The forecast is a scenario distribution from the prior 60-session daily return sample, not a guaranteed result.",
                "Closed-balance drawdown is measured; intrabar floating-equity drawdown and broker stop-out are not reconstructed.",
                "No artificial position-count cap is applied, but the realistic run still respects broker margin.",
                "Scalper setup selection is replayed from each standalone backtest; merged Phoenix PnL does not re-trigger its daily profit lock.",
            ],
        }
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(args.output)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
