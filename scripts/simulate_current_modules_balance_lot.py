from __future__ import annotations

import argparse
import heapq
import json
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
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


def _lot(balance: float, start_balance: float, step_usd: float, maximum: float, symbol: str, cfg: Any) -> float:
    steps = int(max(0.0, balance - start_balance) // step_usd)
    requested = min(maximum, 0.01 + steps * 0.01)
    return normalize_volume(symbol, requested, float(cfg.min_lot), maximum)


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _full_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for row in report.get("equity", []):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        net_001 = float(row.get("profit_001", 0.0) or 0.0) - (
            float(row.get("spread_cost", 0.0) or 0.0) / (original_lot / 0.01)
        )
        events.append(
            {
                "source": "phoenix_full_tp1_tp6",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "net_001": net_001,
            }
        )
    return events


def _range_events(report: dict[str, Any], multiplier: int) -> list[dict[str, Any]]:
    events = []
    for row in report.get("current_live_three_leg_events", []):
        for branch in range(max(1, multiplier)):
            events.append(
                {
                    "source": "phoenix_range" if branch == 0 else "phoenix_range_extra_assumption",
                    "opened": _dt(row["entry_time"]),
                    "closed": _dt(row["exit_time"]),
                    "side": str(row["side"]),
                    "entry": float(row["entry"]),
                    "net_1lot": float(row.get("pnl", 0.0) or 0.0),
                }
            )
    return events


def _scalper_events(label: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for row in report.get("trades", []):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        net_001 = float(row.get("profit_001", 0.0) or 0.0) - (
            float(row.get("spread_cost", 0.0) or 0.0) / (original_lot / 0.01)
        )
        events.append(
            {
                "source": f"scalper_{label}",
                "opened": _dt(row["opened"]),
                "closed": _dt(row["closed"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "net_001": net_001,
            }
        )
    return events


def _simulate(events: list[dict[str, Any]], symbol: str, cfg: Any, start_balance: float, enforce_margin: bool) -> dict[str, Any]:
    balance = start_balance
    peak = balance
    max_dd = 0.0
    used_margin = 0.0
    max_margin = 0.0
    max_positions = 0
    max_lot = 0.0
    accepted = 0
    skipped = 0
    sequence = 0
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    daily: dict[str, float] = defaultdict(float)

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, max_dd, used_margin
        while open_heap and open_heap[0][0] <= moment:
            _, _, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - float(trade["margin"]))
            pnl = float(trade["pnl"])
            balance += pnl
            peak = max(peak, balance)
            max_dd = min(max_dd, balance - peak)
            stats = by_source[str(trade["source"])]
            stats["pnl"] += pnl
            stats["wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"] += 1
            daily[trade["closed"].date().isoformat()] += pnl

    for event in sorted(events, key=lambda item: (item["opened"], item["closed"], item["source"])):
        close_until(event["opened"])
        if balance <= 0.0:
            skipped += 1
            continue
        lot = _lot(balance, start_balance, 500.0, 10.0, symbol, cfg)
        margin = _margin(symbol, event["side"], lot, event["entry"])
        if enforce_margin and margin > balance - used_margin + 0.01:
            skipped += 1
            continue
        pnl = float(event.get("net_001", 0.0)) * (lot / 0.01)
        if "net_1lot" in event:
            pnl = float(event["net_1lot"]) * lot
        trade = {**event, "lot": lot, "margin": margin, "pnl": pnl}
        sequence += 1
        heapq.heappush(open_heap, (trade["closed"], sequence, trade))
        used_margin += margin
        accepted += 1
        max_margin = max(max_margin, used_margin)
        max_positions = max(max_positions, len(open_heap))
        max_lot = max(max_lot, sum(float(item[2]["lot"]) for item in open_heap))
    close_until(datetime.max.replace(tzinfo=UTC))

    sources = {}
    for source, values in sorted(by_source.items()):
        decided = values["wins"] + values["losses"]
        sources[source] = {
            "pnl": round(values["pnl"], 2),
            "wins": int(values["wins"]),
            "losses": int(values["losses"]),
            "flat": int(values["flat"]),
            "win_rate_pct": round(100.0 * values["wins"] / max(1.0, decided), 2),
        }
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
        "max_closed_drawdown_usd": round(max_dd, 2),
        "max_closed_drawdown_pct_start": round(abs(max_dd) / start_balance * 100.0, 2),
        "candidate_positions": len(events),
        "accepted_positions": accepted,
        "margin_skips": skipped,
        "max_concurrent_positions": max_positions,
        "max_concurrent_lot": round(max_lot, 2),
        "max_used_margin": round(max_margin, 2),
        "by_source": sources,
        "daily_pnl": {key: round(value, 2) for key, value in sorted(daily.items())},
    }


def _forecast(result: dict[str, Any], sessions: int, trials: int, start_balance: float) -> dict[str, Any]:
    daily = result.get("daily_pnl", {})
    historical_balance = float(result["start_balance"])
    returns = []
    for day in sorted(daily):
        pnl = float(daily[day])
        returns.append(pnl / historical_balance if historical_balance > 0.0 else -1.0)
        historical_balance += pnl
    if not returns:
        return {}
    rng = random.Random(20260805)
    finals = []
    for _ in range(trials):
        balance = start_balance
        for _ in range(sessions):
            balance *= 1.0 + rng.choice(returns)
            if balance <= 0.0:
                balance = 0.0
                break
        finals.append(balance)
    finals.sort()
    pick = lambda q: round(finals[int((len(finals) - 1) * q)], 2)
    return {
        "start_balance": start_balance,
        "sessions": sessions,
        "p10": pick(0.10),
        "median": pick(0.50),
        "p90": pick(0.90),
        "loss_probability_pct": round(100.0 * sum(value < start_balance for value in finals) / len(finals), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--full", required=True)
    parser.add_argument("--ranges", required=True)
    parser.add_argument("--scalper", action="append", default=[])
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--range-multiplier", type=int, default=1)
    parser.add_argument("--forecast-sessions", type=int, default=60)
    parser.add_argument("--forecast-trials", type=int, default=10000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        events = _full_events(_load(args.full))
        events.extend(_range_events(_load(args.ranges), args.range_multiplier))
        scalper_inputs = {}
        for item in args.scalper:
            label, path = item.split("=", 1)
            scalper_inputs[label] = path
            events.extend(_scalper_events(label, _load(path)))
        realistic = _simulate(events, symbol, cfg, args.start_balance, True)
        theoretical = _simulate(events, symbol, cfg, args.start_balance, False)
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "method": "M1/M5 source reports, chronological shared closed balance, current 0.01 per-leg plus 0.01 per 500 USD profit, spread included, broker margin checked",
            "inputs": {"full": args.full, "ranges": args.ranges, "scalpers": scalper_inputs},
            "range_multiplier": args.range_multiplier,
            "historical_realistic": realistic,
            "historical_without_margin_limit": theoretical,
            "next_sessions": _forecast(realistic, args.forecast_sessions, args.forecast_trials, args.start_balance),
            "limitations": [
                "Phoenix full-signal legs are regenerated with current TP1-TP6 configuration.",
                "The saved Phoenix Range artifact contains three historical legs; range_multiplier=2 mirrors them to estimate the newly added second three legs and is not an independent candle replay.",
                "Closed-balance drawdown is reported; floating intrabar drawdown is not reconstructed.",
            ],
        }
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(args.output)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
