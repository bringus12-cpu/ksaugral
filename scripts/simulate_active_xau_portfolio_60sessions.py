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


def _event(
    source: str,
    opened: str,
    closed: str,
    side: str,
    entry: float,
    net_001: float,
    lot_mode: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "opened": _dt(opened),
        "closed": _dt(closed),
        "side": str(side),
        "entry": float(entry),
        "net_001": float(net_001),
        "lot_mode": lot_mode,
    }


def _phoenix_events(report: dict[str, Any], source: str, lot_mode: str) -> list[dict[str, Any]]:
    return [
        _event(
            source,
            row["entry_time"],
            row["exit_time"],
            row["side"],
            row["entry"],
            row.get("pnl_001", 0.0),
            lot_mode,
        )
        for row in report.get("trades", [])
    ]


def _scalper_events(report: dict[str, Any], source: str, lot_mode: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in report.get("trades", []):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        net_001 = float(row.get("profit_001", 0.0) or 0.0) - (
            float(row.get("spread_cost", 0.0) or 0.0) / (original_lot / 0.01)
        )
        events.append(
            _event(source, row["opened"], row["closed"], row["side"], row["entry"], net_001, lot_mode)
        )
    return events


def _long_term_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for key, strategy in report.get("results", {}).items():
        for row in strategy.get("trades", []):
            events.append(
                _event(
                    f"long_term:{key}",
                    row["entry_time"],
                    row["exit_time"],
                    row["side"],
                    row["entry"],
                    row.get("pnl", 0.0),
                    "fixed_001",
                )
            )
    return events


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _lot_for_mode(mode: str, balance: float, start_balance: float, symbol: str, cfg: Any) -> float:
    if mode == "phoenix_dynamic":
        steps = int(max(0.0, balance - start_balance) // 500.0)
        return normalize_volume(symbol, min(10.0, 0.01 + steps * 0.01), float(cfg.min_lot), 10.0)
    if mode == "phoenix_range_fixed":
        return normalize_volume(symbol, 0.02, float(cfg.min_lot), float(cfg.max_lot))
    if mode == "scalper_core_balance_per_leg":
        steps = max(1, int(max(0.0, balance) // 500.0))
        return normalize_volume(symbol, min(10.0, steps * 0.01), float(cfg.min_lot), 10.0)
    if mode == "scalper_evening_risk3":
        # Active evening profile: 3% setup risk split over six legs with a 3 USD SL.
        order_loss = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, 4000.0, 3997.0) or 0.0)
        )
        risk_per_leg = max(0.0, balance) * 0.03 / 6.0
        raw = max(0.01, risk_per_leg / order_loss) if order_loss > 0.0 else 0.01
        return normalize_volume(symbol, raw, float(cfg.min_lot), float(cfg.max_lot))
    if mode == "fixed_001":
        return normalize_volume(symbol, 0.01, float(cfg.min_lot), float(cfg.max_lot))
    raise ValueError(f"Unknown lot mode: {mode}")


def _simulate(
    events: list[dict[str, Any]],
    symbol: str,
    cfg: Any,
    start_balance: float,
    enforce_margin: bool,
) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_dd = 0.0
    max_dd_pct_peak = 0.0
    used_margin = 0.0
    max_margin = 0.0
    min_balance = balance
    accepted = 0
    margin_skips = 0
    insolvent_skips = 0
    max_positions = 0
    max_lot = 0.0
    sequence = 0
    first_margin_skip: dict[str, Any] | None = None
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_lot_mode: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    overall: dict[str, float] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)
    close_sequence: list[dict[str, Any]] = []

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, max_dd, max_dd_pct_peak, used_margin, min_balance
        while open_heap and open_heap[0][0] <= moment:
            _, _, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - float(trade["margin"]))
            pnl = float(trade["pnl"])
            balance += pnl
            min_balance = min(min_balance, balance)
            peak = max(peak, balance)
            drawdown = balance - peak
            max_dd = min(max_dd, drawdown)
            if peak > 0.0:
                max_dd_pct_peak = max(max_dd_pct_peak, abs(min(0.0, drawdown)) / peak * 100.0)
            for bucket in (overall, by_source[str(trade["source"])], by_lot_mode[str(trade["lot_mode"])]):
                bucket["pnl"] += pnl
                bucket["gross_win"] += max(0.0, pnl)
                bucket["gross_loss"] += min(0.0, pnl)
                bucket["wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"] += 1
                bucket["lot_sum"] += float(trade["lot"])
            daily[trade["closed"].date().isoformat()] += pnl
            close_sequence.append(
                {
                    "closed": trade["closed"].isoformat(),
                    "source": trade["source"],
                    "lot": round(float(trade["lot"]), 2),
                    "pnl": round(pnl, 2),
                    "balance": round(balance, 2),
                }
            )

    for row in sorted(events, key=lambda item: (item["opened"], item["closed"], item["source"])):
        close_until(row["opened"])
        if balance <= 0.0:
            insolvent_skips += 1
            continue
        lot = _lot_for_mode(str(row["lot_mode"]), balance, start_balance, symbol, cfg)
        margin = _margin(symbol, str(row["side"]), lot, float(row["entry"]))
        free_margin_proxy = balance - used_margin
        if enforce_margin and margin > free_margin_proxy + 0.01:
            margin_skips += 1
            if first_margin_skip is None:
                first_margin_skip = {
                    "time": row["opened"].isoformat(),
                    "source": row["source"],
                    "lot": lot,
                    "required_margin": round(margin, 2),
                    "free_margin_proxy": round(free_margin_proxy, 2),
                    "balance": round(balance, 2),
                }
            continue
        pnl = float(row["net_001"]) * (lot / 0.01)
        trade = {**row, "lot": lot, "margin": margin, "pnl": pnl}
        sequence += 1
        heapq.heappush(open_heap, (row["closed"], sequence, trade))
        used_margin += margin
        accepted += 1
        max_margin = max(max_margin, used_margin)
        max_positions = max(max_positions, len(open_heap))
        max_lot = max(max_lot, sum(float(item[2]["lot"]) for item in open_heap))
    close_until(datetime.max.replace(tzinfo=UTC))

    def format_bucket(values: dict[str, float]) -> dict[str, Any]:
        positions = int(values["wins"] + values["losses"] + values["flat"])
        decided = int(values["wins"] + values["losses"])
        gross_loss = abs(float(values["gross_loss"]))
        return {
            "positions": positions,
            "wins": int(values["wins"]),
            "losses": int(values["losses"]),
            "flat": int(values["flat"]),
            "win_rate_pct": round(100.0 * values["wins"] / max(1, decided), 2),
            "pnl": round(values["pnl"], 2),
            "profit_factor": round(values["gross_win"] / gross_loss, 3) if gross_loss > 0.0 else None,
            "average_lot": round(values["lot_sum"] / max(1, positions), 3),
        }

    formatted_daily = {key: round(value, 2) for key, value in sorted(daily.items())}
    worst_day = min(formatted_daily.items(), key=lambda item: item[1]) if formatted_daily else None
    best_day = max(formatted_daily.items(), key=lambda item: item[1]) if formatted_daily else None
    positive_days = sum(value > 0.005 for value in formatted_daily.values())
    negative_days = sum(value < -0.005 for value in formatted_daily.values())
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "net_profit": round(balance - start_balance, 2),
        "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
        "minimum_closed_balance": round(min_balance, 2),
        "max_closed_drawdown_usd": round(max_dd, 2),
        "max_closed_drawdown_pct_of_peak": round(max_dd_pct_peak, 2),
        "candidate_positions": len(events),
        "accepted_positions": accepted,
        "margin_skips": margin_skips,
        "insolvent_skips": insolvent_skips,
        "first_margin_skip": first_margin_skip,
        "max_concurrent_positions": max_positions,
        "max_concurrent_lot": round(max_lot, 2),
        "max_used_margin": round(max_margin, 2),
        "overall_positions": format_bucket(overall),
        "days_with_closed_trades": len(formatted_daily),
        "positive_days": positive_days,
        "negative_days": negative_days,
        "daily_win_rate_pct": round(100.0 * positive_days / max(1, positive_days + negative_days), 2),
        "average_daily_pnl": round(sum(formatted_daily.values()) / max(1, len(formatted_daily)), 2),
        "best_day": {"date": best_day[0], "pnl": best_day[1]} if best_day else None,
        "worst_day": {"date": worst_day[0], "pnl": worst_day[1]} if worst_day else None,
        "by_source": {key: format_bucket(value) for key, value in sorted(by_source.items())},
        "by_lot_mode": {key: format_bucket(value) for key, value in sorted(by_lot_mode.items())},
        "daily_pnl": formatted_daily,
        "close_sequence": close_sequence,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--phoenix", required=True)
    parser.add_argument("--phoenix-range", required=True)
    parser.add_argument("--core", required=True)
    parser.add_argument("--evening", required=True)
    parser.add_argument("--long-term", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(Path(args.env).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        phoenix = _load(args.phoenix)
        phoenix_range = _load(args.phoenix_range)
        core = _load(args.core)
        evening = _load(args.evening)
        long_term = _load(args.long_term)
        events = _phoenix_events(phoenix, "phoenix_full", "phoenix_dynamic")
        events.extend(_phoenix_events(phoenix_range, "phoenix_pre_range", "phoenix_range_fixed"))
        events.extend(_scalper_events(core, "scalper_core", "scalper_core_balance_per_leg"))
        events.extend(_scalper_events(evening, "scalper_evening", "scalper_evening_risk3"))
        events.extend(_long_term_events(long_term))

        realistic = _simulate(events, symbol, cfg, float(args.start_balance), True)
        theoretical = _simulate(events, symbol, cfg, float(args.start_balance), False)
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": phoenix.get("range_utc"),
            "sessions_included": phoenix.get("sessions_included", []),
            "symbol": symbol,
            "method": (
                "Chronological shared closed balance from 1000 USD; current Phoenix full and preliminary range, "
                "core/evening scalpers and eight enabled long-term XAU strategies; per-module live lot rules, M1/M5/H1/D1 "
                "source replays, spread and modeled commission included, concurrent positions retained"
            ),
            "lot_rules": {
                "phoenix_full": "0.01 per leg at 1000 USD, +0.01 per completed 500 USD profit step",
                "phoenix_pre_range": "fixed 0.02 per leg (range balance scaling is disabled in active profile)",
                "scalper_core": "0.01 per leg for each full 500 USD of current balance; 0.02 at 1000 USD",
                "scalper_evening": "3% setup risk divided over six legs with 3 USD stop; broker volume rounding",
                "long_term": "fixed 0.01 per strategy position",
            },
            "inputs": {
                "phoenix": args.phoenix,
                "phoenix_range": args.phoenix_range,
                "core": args.core,
                "evening": args.evening,
                "long_term": args.long_term,
            },
            "realistic_margin": realistic,
            "without_margin_limit": theoretical,
            "limitations": [
                "Drawdown is calculated from chronologically closed balance; intrabar floating equity drawdown is not reconstructed.",
                "Margin admission uses closed balance minus reserved margin as a conservative proxy; it does not mark open positions to market.",
                "Phoenix preliminary ranges are replayed independently from later full signals; live reconcile/stale-profit edits cannot be reconstructed perfectly from candles alone.",
                "This is an in-sample historical reconstruction, not a profit forecast or guarantee.",
            ],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        brief = {key: value for key, value in realistic.items() if key not in {"daily_pnl", "close_sequence", "by_lot_mode"}}
        print(json.dumps({"output": str(out), "result": brief}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
