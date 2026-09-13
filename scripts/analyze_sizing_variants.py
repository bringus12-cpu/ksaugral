from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


VARIANTS = {
    "dynamic_no_martingale": (1.0, 0, 1.0),
    "dynamic_bounded_1_25": (1.25, 1, 1.25),
    "dynamic_bounded_1_50": (1.50, 1, 1.50),
    "dynamic_two_steps_1_25": (1.25, 2, 1.50),
    "dynamic_classic_2x": (2.0, 3, 8.0),
}


def _base_stop_loss(trade: dict) -> float:
    entry = float(trade["entry"])
    exit_price = float(trade["exit"])
    stop = float(trade["sl"])
    profit = abs(float(trade["profit"]))
    realized_move = abs(exit_price - entry)
    stop_move = abs(stop - entry)
    if realized_move <= 0.0 or profit <= 0.0:
        return max(0.01, profit)
    return max(0.01, profit * stop_move / realized_move)


def _stats(values: list[float], balances: list[float]) -> dict:
    wins = [value for value in values if value > 0.0]
    losses = [value for value in values if value < 0.0]
    peaks = list(itertools.accumulate(balances, max)) if balances else []
    drawdowns = [peak - balance for peak, balance in zip(peaks, balances)]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(values),
        "pnl": round(sum(values), 2),
        "ending_balance": round(balances[-1], 2) if balances else 0.0,
        "win_rate_pct": round(100.0 * len(wins) / max(1, len(values)), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown": round(max(drawdowns, default=0.0), 2),
        "max_closed_drawdown_pct_start": round(100.0 * max(drawdowns, default=0.0) / max(1.0, balances[0]), 2) if balances else 0.0,
        "ruined": bool(balances and min(balances) <= 0.0),
    }


def _simulate(trades: list[dict], starting_balance: float, risk_pct: float, factor: float, steps: int, cap: float, max_lot: float) -> dict:
    balance = starting_balance
    balances = [balance]
    values: list[float] = []
    streaks: dict[str, int] = defaultdict(int)
    maximum_multiplier = 1.0
    for trade in sorted(trades, key=lambda item: item["entry_time"]):
        key = f"{trade['symbol']}::{trade['strategy']}"
        stop_loss = _base_stop_loss(trade)
        risk_amount = max(0.0, balance) * risk_pct / 100.0
        risk_scale = max(1.0, risk_amount / stop_loss)
        recovery = min(cap, factor ** min(streaks[key], steps)) if steps > 0 else 1.0
        base_volume = max(0.00001, float(trade.get("volume", 0.01) or 0.01))
        lot_scale_cap = max(1.0, max_lot / base_volume)
        scale = min(lot_scale_cap, risk_scale * recovery)
        maximum_multiplier = max(maximum_multiplier, scale)
        value = float(trade["profit"]) * scale
        balance += value
        values.append(value)
        balances.append(balance)
        streaks[key] = streaks[key] + 1 if value < 0.0 else 0
    result = _stats(values, balances)
    result["maximum_lot_scale"] = round(maximum_multiplier, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare dynamic lot and bounded martingale variants")
    parser.add_argument("report")
    parser.add_argument("--start", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=0.10)
    parser.add_argument("--max-lot", type=float, default=0.50)
    parser.add_argument("--pairs-from", default="")
    parser.add_argument("--max-recommended-dd-pct", type=float, default=30.0)
    parser.add_argument("--output", default="data_vantage/market_machine_sizing_analysis.json")
    args = parser.parse_args()

    payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    selected_pairs = set(payload.get("research_promotion_candidates", []))
    if args.pairs_from:
        analytics = json.loads(Path(args.pairs_from).read_text(encoding="utf-8"))
        selected_pairs = set(analytics.get("diversified_pairs") or analytics.get("stable_pairs", []))
    trades = [
        trade
        for trade in payload.get("trades", [])
        if f"{trade['symbol']}::{trade['strategy']}" in selected_pairs
    ]
    results = {
        name: _simulate(trades, args.start, args.risk_pct, factor, steps, cap, args.max_lot)
        for name, (factor, steps, cap) in VARIANTS.items()
    }
    eligible = [
        name
        for name, result in results.items()
        if not result["ruined"] and result["max_closed_drawdown_pct_start"] <= args.max_recommended_dd_pct
    ]
    research_winner = (
        max(eligible, key=lambda key: (results[key]["ending_balance"], -results[key]["max_closed_drawdown"]))
        if eligible
        else min(results, key=lambda key: (results[key]["ruined"], results[key]["max_closed_drawdown"]))
    )
    recommendation = (
        "dynamic_no_martingale"
        if not results["dynamic_no_martingale"]["ruined"]
        else min(results, key=lambda key: (results[key]["ruined"], results[key]["max_closed_drawdown"]))
    )
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source": str(Path(args.report).resolve()),
        "selection": (
            "correlation-diversified stable pairs from robustness analytics"
            if args.pairs_from
            else "positive instrument-strategy pairs from source report"
        ),
        "starting_balance": args.start,
        "risk_per_trade_pct": args.risk_pct,
        "max_lot": args.max_lot,
        "selected_pairs": sorted(selected_pairs),
        "results": results,
        "max_recommended_dd_pct": args.max_recommended_dd_pct,
        "research_winner": research_winner,
        "recommendation": recommendation,
        "martingale_conclusion": (
            "research_only; live martingale remains disabled because recovery sizing increases tail drawdown"
        ),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"REPORT={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
