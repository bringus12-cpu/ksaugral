from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from optimize_walk_forward_portfolio import _run
from simulate_risk_portfolio import _bounded_r


PLANS = {
    "equal": (1.0, 1.0, 1.0),
    "front_50_30_20": (0.50, 0.30, 0.20),
    "high_win_70_25_05": (0.70, 0.25, 0.05),
    "balanced_60_25_15": (0.60, 0.25, 0.15),
}


def _category(target_index: int) -> int:
    if target_index <= 1:
        return 0
    if target_index <= 3:
        return 1
    return 2


def _events(path: Path, spread: float, plan: tuple[float, float, float]) -> list[dict[str, Any]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for leg in report.get("equity", []):
        grouped[(str(leg["signal_time"]), str(leg["channel"]), int(leg.get("message_id", 0) or 0))].append(leg)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float, float]] = set()
    for (signal_time, channel, _), legs in sorted(grouped.items()):
        categories: dict[int, list[float]] = defaultdict(list)
        for leg in legs:
            risk = abs(float(leg["entry"]) - float(leg["initial_sl"])) + spread
            if risk <= 0:
                continue
            leg_r = (float(leg["profit_001"]) - spread) / risk
            categories[_category(int(leg.get("target_index", 1) or 1))].append(_bounded_r(leg_r, 4.0, 1.05))
        active = [index for index in range(3) if categories[index]]
        if not active:
            continue
        if plan == PLANS["equal"]:
            all_values = [value for values in categories.values() for value in values]
            signal_r = mean(all_values)
        else:
            active_weight = sum(plan[index] for index in active)
            signal_r = sum((plan[index] / active_weight) * mean(categories[index]) for index in active)
        opened = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
        bucket = opened.replace(minute=(opened.minute // 5) * 5, second=0, microsecond=0).isoformat()
        first = legs[0]
        fingerprint = (bucket, str(first.get("side", "")), round(float(first["entry"]), 1), round(float(first["initial_sl"]), 1))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        output.append(
            {
                "opened": signal_time,
                "cluster": signal_time[:16],
                "source": "telegram",
                "strategy": channel,
                "legs": len(legs),
                "r": _bounded_r(signal_r, 4.0, 1.05),
            }
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", required=True)
    parser.add_argument("--channel", action="append", default=[])
    parser.add_argument("--spread-price", type=float, default=0.18)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--daily-loss-pct", type=float, default=1.5)
    parser.add_argument("--daily-profit-pct", type=float, default=3.0)
    parser.add_argument("--max-consecutive-losses", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    results: dict[str, Any] = {}
    for name, plan in PLANS.items():
        events = [item for item in _events(Path(args.telegram), args.spread_price, plan) if not args.channel or item["strategy"] in args.channel]
        start = min(datetime.fromisoformat(item["opened"].replace("Z", "+00:00")) for item in events)
        split = start + timedelta(days=40)
        train = [item for item in events if datetime.fromisoformat(item["opened"].replace("Z", "+00:00")) < split]
        test = [item for item in events if datetime.fromisoformat(item["opened"].replace("Z", "+00:00")) >= split]
        policy = {
            "risk_pct": args.risk_pct,
            "daily_loss_pct": args.daily_loss_pct,
            "daily_profit_pct": args.daily_profit_pct,
            "max_consecutive_losses": args.max_consecutive_losses,
        }
        results[name] = {
            "plan": plan,
            "train": _run(train, 1000.0, **policy),
            "holdout": _run(test, 1000.0, **policy),
            "full": _run(events, 1000.0, **policy),
        }
    Path(args.output).write_text(json.dumps(results, indent=2, ensure_ascii=True), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
