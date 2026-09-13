from __future__ import annotations

import argparse
import heapq
import json
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


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _normal(symbol: str, cfg: Any, lot: float) -> float:
    return normalize_volume(symbol, lot, float(cfg.min_lot), max(float(cfg.max_lot), 10.0))


def _events(full: dict[str, Any], ranges: dict[str, Any], direction: dict[str, Any], scalper: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for source, report in (("phoenix_full", full), ("phoenix_range", ranges), ("phoenix_direction", direction)):
        for row in report.get("trades", []):
            events.append(
                {
                    "source": source,
                    "opened": _dt(row["entry_time"]),
                    "closed": _dt(row["exit_time"]),
                    "side": str(row["side"]),
                    "entry": float(row["entry"]),
                    "pnl_001": float(row["pnl_001"]),
                    "status": str(row.get("status", "")),
                    "signal_id": int(row.get("message_id", 0) or 0),
                }
            )
    for row in scalper.get("trades", []):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        scale = original_lot / 0.01
        net_001 = (
            float(row.get("profit_001", 0.0))
            - float(row.get("spread_cost", 0.0)) / scale
            - float(row.get("commission", 0.0)) / scale
        )
        events.append(
            {
                "source": "scalper",
                "opened": _dt(row["opened"]),
                "closed": _dt(row["closed"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "pnl_001": net_001,
                "status": str(row.get("status", "")),
                "signal_id": str(row.get("opened", "")),
                "setup_tag": str(row.get("setup_tag", "")),
            }
        )
    return sorted(events, key=lambda row: (row["opened"], row["source"], str(row["signal_id"])))


def _lot(source: str, balance: float, start_balance: float) -> float:
    if source == "phoenix_full":
        return 0.01 + max(0, int((balance - 1000.0) // 500.0)) * 0.01
    if source == "scalper":
        return max(1, int(max(0.0, balance) // 500.0)) * 0.01
    if source == "phoenix_range":
        return 0.02
    return 0.01


def _simulate(symbol: str, cfg: Any, events: list[dict[str, Any]], start_balance: float, fixed_001: bool = False) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_dd = 0.0
    used_margin = 0.0
    max_used_margin = 0.0
    max_concurrent = 0
    max_concurrent_lot = 0.0
    margin_skips = 0
    accepted = 0
    sequence = 0
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_day: dict[str, float] = defaultdict(float)
    legs: list[dict[str, Any]] = []

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, max_dd, used_margin
        while open_heap and open_heap[0][0] <= moment:
            _closed, _seq, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - float(trade["margin"]))
            pnl = float(trade["pnl"])
            balance += pnl
            peak = max(peak, balance)
            max_dd = min(max_dd, balance - peak)
            source = str(trade["source"])
            row = by_source[source]
            row["pnl"] += pnl
            row["legs"] += 1
            row["wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"] += 1
            by_day[trade["closed"].date().isoformat()] += pnl
            legs.append({**trade, "balance_after": round(balance, 2)})

    for event in events:
        close_until(event["opened"])
        lot = 0.01 if fixed_001 else _lot(str(event["source"]), balance, float(start_balance))
        lot = _normal(symbol, cfg, lot)
        margin = _margin(symbol, event["side"], lot, event["entry"])
        if balance <= 0.0 or margin > max(0.0, balance - used_margin) + 0.01:
            margin_skips += 1
            continue
        pnl = float(event["pnl_001"]) * (lot / 0.01)
        trade = {**event, "lot": lot, "margin": margin, "pnl": pnl}
        sequence += 1
        heapq.heappush(open_heap, (event["closed"], sequence, trade))
        used_margin += margin
        max_used_margin = max(max_used_margin, used_margin)
        max_concurrent = max(max_concurrent, len(open_heap))
        max_concurrent_lot = max(max_concurrent_lot, sum(float(item[2]["lot"]) for item in open_heap))
        accepted += 1
    close_until(datetime.max.replace(tzinfo=UTC))

    source_summary: dict[str, Any] = {}
    for source, row in sorted(by_source.items()):
        wins, losses = int(row["wins"]), int(row["losses"])
        source_summary[source] = {
            "pnl": round(row["pnl"], 2),
            "legs": int(row["legs"]),
            "wins": wins,
            "losses": losses,
            "flat": int(row["flat"]),
            "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        }
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "max_closed_drawdown_usd": round(max_dd, 2),
        "max_closed_drawdown_pct_start": round(100.0 * abs(max_dd) / start_balance, 2),
        "candidate_legs": len(events),
        "accepted_legs": accepted,
        "margin_skips": margin_skips,
        "max_concurrent_positions": max_concurrent,
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "max_used_margin": round(max_used_margin, 2),
        "active_days": len(by_day),
        "profitable_days": sum(value > 0.0 for value in by_day.values()),
        "losing_days": sum(value < 0.0 for value in by_day.values()),
        "by_source": source_summary,
        "daily_pnl": {key: round(value, 2) for key, value in sorted(by_day.items())},
        "closed_legs": sorted(legs, key=lambda row: row["closed"]),
    }


def _period_stats(events: list[dict[str, Any]], sessions: list[str]) -> dict[str, Any]:
    midpoint = len(sessions) // 2
    periods = {"first_30_sessions": set(sessions[:midpoint]), "last_30_sessions": set(sessions[midpoint:])}
    result: dict[str, Any] = {}
    for label, dates in periods.items():
        rows = [row for row in events if (row["opened"] + __import__("datetime").timedelta(hours=2)).date().isoformat() in dates]
        by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in rows:
            value = float(row["pnl_001"])
            source = str(row["source"])
            by_source[source]["pnl_001"] += value
            by_source[source]["wins" if value > 0.005 else "losses" if value < -0.005 else "flat"] += 1
        result[label] = {
            source: {
                "pnl_at_001": round(values["pnl_001"], 2),
                "wins": int(values["wins"]),
                "losses": int(values["losses"]),
                "win_rate_pct": round(100.0 * values["wins"] / max(1, values["wins"] + values["losses"]), 2),
            }
            for source, values in sorted(by_source.items())
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--full", required=True)
    parser.add_argument("--ranges", required=True)
    parser.add_argument("--direction", required=True)
    parser.add_argument("--scalper", required=True)
    parser.add_argument("--actual-balance", type=float, default=0.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        full, ranges, direction, scalper = map(_load, (args.full, args.ranges, args.direction, args.scalper))
        events = _events(full, ranges, direction, scalper)
        sessions = list(full.get("sessions_included", []))
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": full.get("range_utc"),
            "sessions_included": sessions,
            "symbol": symbol,
            "method": (
                "Exact current active module outputs merged chronologically on one balance; historical M1 spread and commission "
                "are inherited from source tests, active per-module lot formulas are recalculated at every entry, broker margin is enforced"
            ),
            "normalized_start_1000_dynamic": _simulate(symbol, cfg, events, 1000.0),
            "normalized_start_1000_fixed_001": _simulate(symbol, cfg, events, 1000.0, fixed_001=True),
            "actual_balance_dynamic": _simulate(symbol, cfg, events, float(args.actual_balance)) if args.actual_balance > 0 else None,
            "stability_fixed_001": _period_stats(events, sessions),
            "source_reports": {
                "phoenix_full": full.get("summary"),
                "phoenix_range": ranges.get("summary"),
                "phoenix_direction": direction.get("summary"),
                "scalper": {
                    key: scalper.get(key)
                    for key in (
                        "start_balance",
                        "final_balance",
                        "profit",
                        "profit_percent",
                        "max_drawdown_from_peak",
                        "batches",
                        "legs_closed",
                        "win_rate_closed_legs_pct",
                        "profit_factor",
                        "spread_paid",
                        "commission_paid",
                    )
                },
            },
            "limitations": [
                "Telegram and MT5 timestamps are aligned to the broker clock and execution starts on the next complete M1 bar.",
                "When TP and SL occur in the same M1 candle, the loss is counted first.",
                "Closed-balance drawdown is exact for reconstructed exits; tick-level floating drawdown and slippage beyond M1 spread are not available.",
                "The test reuses the current rules on old data; it is not an independent out-of-sample validation because those rules were tuned using part of this history.",
            ],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        print(json.dumps({
            "output": str(out),
            "dynamic_1000": {key: output["normalized_start_1000_dynamic"][key] for key in ("final_balance", "profit", "return_pct", "max_closed_drawdown_usd", "margin_skips")},
            "fixed_001": {key: output["normalized_start_1000_fixed_001"][key] for key in ("final_balance", "profit", "return_pct", "max_closed_drawdown_usd", "margin_skips")},
            "actual": ({key: output["actual_balance_dynamic"][key] for key in ("start_balance", "final_balance", "profit", "return_pct", "max_closed_drawdown_usd", "margin_skips")} if output["actual_balance_dynamic"] else None),
        }, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
