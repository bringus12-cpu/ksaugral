from __future__ import annotations

import argparse
import heapq
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
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


def _normal(symbol: str, cfg: Any, value: float, maximum: float = 999.0) -> float:
    return normalize_volume(symbol, value, float(cfg.min_lot), max(float(cfg.max_lot), maximum))


def _covered_signal_ids(listener: dict[str, Any], ranges: dict[str, Any]) -> set[int]:
    candidates = {}
    for row in ranges.get("current_live_three_leg_events", []):
        candidates[int(row["range_message_id"])] = {
            "time": _dt(row["signal_time"]),
            "side": str(row["side"]),
        }
    covered = set()
    for row in listener.get("equity", []):
        signal_time = _dt(row["signal_time"])
        for candidate in candidates.values():
            delay = signal_time - candidate["time"]
            if (
                str(row["side"]) == candidate["side"]
                and timedelta(0) <= delay <= timedelta(minutes=5)
            ):
                covered.add(int(row["message_id"]))
                break
    return covered


def _events(listener: dict[str, Any], ranges: dict[str, Any], scalper: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    covered = _covered_signal_ids(listener, ranges)
    output = []
    for row in listener.get("equity", []):
        message_id = int(row["message_id"])
        if message_id in covered:
            continue
        output.append(
            {
                "source": "phoenix_full_runner" if int(row.get("plan_index", 0)) == 999 else "phoenix_full",
                "setup": f"phoenix:{message_id}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["initial_sl"]),
                "profit_001": float(row["profit_001"]),
                "spread_per_001": float(row.get("spread_cost", 0.0)),
                "plan_index": int(row.get("plan_index", 0)),
            }
        )
    for row in ranges.get("current_live_three_leg_events", []):
        output.append(
            {
                "source": "phoenix_range",
                "setup": f"range:{int(row['range_message_id'])}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["entry"]) - 6.0 if str(row["side"]) == "buy" else float(row["entry"]) + 6.0,
                "pnl_1lot_net": float(row["pnl"]),
                "plan_index": int(row["leg_index"]),
            }
        )
    for row in scalper.get("trades", []):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01)))
        output.append(
            {
                "source": "scalper",
                "setup": f"scalper:{row['opened']}",
                "opened": _dt(row["opened"]),
                "closed": _dt(row["closed"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": 0.0,
                "profit_001": float(row["profit_001"]),
                "spread_per_001": float(row.get("spread_cost", 0.0)) / (original_lot / 0.01),
                "plan_index": 0,
            }
        )
    output.sort(key=lambda row: (row["opened"], row["setup"], row["plan_index"]))
    return output, {
        "covered_full_signal_messages": len(covered),
        "listener_legs_removed_as_preliminary_duplicates": sum(
            1 for row in listener.get("equity", []) if int(row["message_id"]) in covered
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--listener", required=True)
    parser.add_argument("--ranges", required=True)
    parser.add_argument("--scalper", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        listener = _load(args.listener)
        ranges = _load(args.ranges)
        scalper = _load(args.scalper)
        events, reconciliation = _events(listener, ranges, scalper)

        setup_counts: dict[str, int] = defaultdict(int)
        for event in events:
            setup_counts[event["setup"]] += 1

        balance = float(args.start_balance)
        peak = balance
        max_dd = 0.0
        max_dd_pct_peak = 0.0
        used_margin = 0.0
        max_used_margin = 0.0
        max_concurrent = 0
        max_concurrent_lot = 0.0
        accepted = 0
        margin_skips = 0
        first_margin_skip = None
        open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
        sequence = 0
        setup_reference_lot: dict[str, float] = {}
        by_source: dict[str, float] = defaultdict(float)
        source_legs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        daily_pnl: dict[str, float] = defaultdict(float)
        lot_ranges: dict[str, list[float]] = defaultdict(list)

        def close_until(moment: datetime) -> None:
            nonlocal balance, peak, max_dd, max_dd_pct_peak, used_margin
            while open_heap and open_heap[0][0] <= moment:
                _closed, _seq, trade = heapq.heappop(open_heap)
                used_margin = max(0.0, used_margin - float(trade["margin"]))
                pnl = float(trade["pnl"])
                balance += pnl
                peak = max(peak, balance)
                max_dd = min(max_dd, balance - peak)
                if peak > 0:
                    max_dd_pct_peak = max(max_dd_pct_peak, (peak - balance) / peak * 100.0)
                by_source[trade["source"]] += pnl
                daily_pnl[trade["closed"].date().isoformat()] += pnl
                source_legs[trade["source"]]["wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"] += 1

        for event in events:
            close_until(event["opened"])
            source = str(event["source"])
            if source == "scalper":
                steps = max(1, int(max(0.0, balance) // 200.0))
                lot = _normal(symbol, cfg, steps * 0.01)
            elif source == "phoenix_range":
                risk_per_leg = max(0.0, balance) * 0.005 / 3.0
                loss = _loss_per_lot(symbol, event["side"], event["entry"], event["sl"])
                lot = _normal(symbol, cfg, risk_per_leg / loss if loss > 0 else cfg.min_lot, 0.10)
            elif source == "phoenix_full":
                risk_per_leg = max(0.0, balance) * 0.01 / max(1, setup_counts[event["setup"]])
                loss = _loss_per_lot(symbol, event["side"], event["entry"], event["sl"])
                lot = _normal(symbol, cfg, risk_per_leg / loss if loss > 0 else cfg.min_lot)
                setup_reference_lot.setdefault(event["setup"], lot)
            else:
                ordinary = setup_reference_lot.get(event["setup"], float(cfg.min_lot))
                lot = _normal(symbol, cfg, ordinary * 10.0)

            margin = _margin(symbol, event["side"], lot, event["entry"])
            free_margin = max(0.0, balance - used_margin)
            if balance <= 0 or margin > free_margin + 0.01:
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

            if source == "phoenix_range":
                pnl = float(event["pnl_1lot_net"]) * lot
            else:
                scale = lot / 0.01
                pnl = float(event["profit_001"]) * scale - float(event["spread_per_001"]) * scale
            trade = {**event, "lot": lot, "margin": margin, "pnl": pnl}
            sequence += 1
            heapq.heappush(open_heap, (trade["closed"], sequence, trade))
            used_margin += margin
            accepted += 1
            lot_ranges[source].append(lot)
            max_used_margin = max(max_used_margin, used_margin)
            max_concurrent = max(max_concurrent, len(open_heap))
            max_concurrent_lot = max(max_concurrent_lot, sum(float(row[2]["lot"]) for row in open_heap))

        close_until(datetime.max.replace(tzinfo=UTC))
        source_summary = {}
        for source in sorted(set(by_source) | set(source_legs)):
            counts = source_legs[source]
            decided = counts["wins"] + counts["losses"]
            lots = lot_ranges[source]
            source_summary[source] = {
                "pnl": round(by_source[source], 2),
                "wins": counts["wins"],
                "losses": counts["losses"],
                "flat": counts["flat"],
                "win_rate_pct": round(100.0 * counts["wins"] / max(1, decided), 2),
                "min_lot": round(min(lots), 2) if lots else 0.0,
                "max_lot": round(max(lots), 2) if lots else 0.0,
            }

        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": listener.get("range_utc"),
            "method": "M1, broker clock +3h for Telegram, historical spread deducted, chronological shared balance, margin enforced",
            "start_balance": round(float(args.start_balance), 2),
            "final_balance": round(balance, 2),
            "profit": round(balance - float(args.start_balance), 2),
            "return_pct": round((balance / float(args.start_balance) - 1.0) * 100.0, 2),
            "max_closed_drawdown_usd": round(max_dd, 2),
            "max_closed_drawdown_pct_of_start": round(abs(max_dd) / float(args.start_balance) * 100.0, 2),
            "max_closed_drawdown_pct_of_peak": round(max_dd_pct_peak, 2),
            "candidate_legs": len(events),
            "accepted_legs": accepted,
            "skipped_for_margin": margin_skips,
            "first_margin_skip": first_margin_skip,
            "max_concurrent_positions": max_concurrent,
            "max_concurrent_lot": round(max_concurrent_lot, 2),
            "max_used_margin": round(max_used_margin, 2),
            "reconciliation": reconciliation,
            "by_source": source_summary,
            "active_days": len(daily_pnl),
            "profitable_days": sum(1 for value in daily_pnl.values() if value > 0.0),
            "losing_days": sum(1 for value in daily_pnl.values() if value < 0.0),
            "best_day": max(daily_pnl.items(), key=lambda row: row[1]) if daily_pnl else None,
            "worst_day": min(daily_pnl.items(), key=lambda row: row[1]) if daily_pnl else None,
            "daily_pnl": {key: round(value, 2) for key, value in sorted(daily_pnl.items())},
            "sizing": {
                "scalper": "0.01 lot per leg per every 200 USD of current closed balance, 6 legs",
                "phoenix_range": "0.5% balance risk split over 3 legs, 0.10 max per leg",
                "phoenix_full": "1.0% balance risk divided by all planned legs",
                "phoenix_tp1_runner": "10x ordinary Phoenix full-signal leg",
            },
            "limitations": [
                "Closed-balance drawdown is reported; intrabar floating-equity drawdown is not reconstructed.",
                "Scalper setup selection comes from its own historical profit-lock replay; the merged Phoenix PnL does not re-trigger that lock.",
                "Broker margin is checked against closed balance minus reserved margin; floating PnL is not added to free margin.",
            ],
        }
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(args.output)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
