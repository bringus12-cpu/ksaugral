from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from statistics import mean
from typing import Any

from simulate_risk_portfolio import _scalper_events, _telegram_events


def _parse_time(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(event["opened"]).replace("Z", "+00:00"))


def _stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["r"]) for item in events]
    positive = sum(value > 0 for value in values)
    return {
        "setups": len(values),
        "mean_r": round(mean(values), 4) if values else 0.0,
        "sum_r": round(sum(values), 2),
        "positive_pct": round(100.0 * positive / max(1, len(values)), 2),
    }


def _select_strategies(train: list[dict[str, Any]]) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in train:
        grouped[(str(event["source"]), str(event["strategy"]))].append(event)
    diagnostics: dict[str, dict[str, Any]] = {}
    telegram: list[str] = []
    scalpers: list[str] = []
    for (source, strategy), events in grouped.items():
        row = _stats(events)
        diagnostics[f"{source}:{strategy}"] = row
        minimum = 8 if source == "telegram" else 25
        if row["setups"] >= minimum and row["mean_r"] > 0.03 and row["positive_pct"] >= 50.0:
            (telegram if source == "telegram" else scalpers).append(strategy)
    return sorted(telegram), sorted(scalpers), diagnostics


def _clusters(events: list[dict[str, Any]]) -> list[tuple[str, float, int]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for event in events:
        grouped[str(event["cluster"])].append(float(event["r"]))
    return sorted((key, mean(values), len(values)) for key, values in grouped.items())


def _run(
    events: list[dict[str, Any]],
    start_balance: float,
    risk_pct: float,
    daily_loss_pct: float,
    daily_profit_pct: float,
    max_consecutive_losses: int,
) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_dd = 0.0
    day = ""
    day_start = balance
    consecutive_losses = 0
    stopped_days: set[str] = set()
    traded = skipped = wins = losses = 0
    daily_returns: dict[str, float] = {}
    for stamp, r_value, _count in _clusters(events):
        current_day = stamp[:10]
        if current_day != day:
            if day:
                daily_returns[day] = (balance / day_start - 1.0) * 100.0
            day = current_day
            day_start = balance
            consecutive_losses = 0
        day_return = (balance / day_start - 1.0) * 100.0 if day_start > 0 else -100.0
        if (
            current_day in stopped_days
            or day_return <= -daily_loss_pct
            or day_return >= daily_profit_pct
            or consecutive_losses >= max_consecutive_losses
        ):
            stopped_days.add(current_day)
            skipped += 1
            continue
        balance *= 1.0 + (risk_pct / 100.0 * r_value)
        balance = max(0.0, balance)
        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak * 100.0 if peak > 0 else 100.0)
        traded += 1
        if r_value > 0:
            wins += 1
            consecutive_losses = 0
        elif r_value < 0:
            losses += 1
            consecutive_losses += 1
    if day:
        daily_returns[day] = (balance / day_start - 1.0) * 100.0
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
        "max_closed_equity_dd_pct": round(max_dd, 2),
        "traded_clusters": traded,
        "skipped_clusters": skipped,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "positive_days": sum(value > 0 for value in daily_returns.values()),
        "negative_days": sum(value < 0 for value in daily_returns.values()),
        "max_daily_loss_pct": round(min(daily_returns.values()), 2) if daily_returns else 0.0,
        "max_daily_gain_pct": round(max(daily_returns.values()), 2) if daily_returns else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", required=True)
    parser.add_argument("--scalper", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--spread-price", type=float, default=0.18)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    telegram = _telegram_events(Path(args.telegram), args.spread_price, 4.0, 1.05)
    scalpers: list[dict[str, Any]] = []
    for raw in args.scalper:
        label, path = raw.split("=", 1)
        scalpers.extend(_scalper_events(Path(path), label, 4.0, 1.05))
    all_events = sorted(telegram + scalpers, key=_parse_time)
    start = min(_parse_time(item) for item in all_events)
    split = start + timedelta(days=40)
    train = [item for item in all_events if _parse_time(item) < split]
    test = [item for item in all_events if _parse_time(item) >= split]

    telegram_selected, scalpers_selected, diagnostics = _select_strategies(train)
    _, _, holdout_diagnostics = _select_strategies(test)
    _, _, full_diagnostics = _select_strategies(all_events)
    selected = [
        item
        for item in all_events
        if (item["source"] == "telegram" and item["strategy"] in telegram_selected)
        or (item["source"] == "scalper" and item["strategy"] in scalpers_selected)
    ]
    selected_train = [item for item in selected if _parse_time(item) < split]
    selected_test = [item for item in selected if _parse_time(item) >= split]

    candidates: list[dict[str, Any]] = []
    for risk, daily_loss, daily_profit, consecutive in product(
        (0.25, 0.5, 0.75, 1.0),
        (1.0, 1.5, 2.0, 3.0),
        (1.0, 1.5, 2.0, 3.0),
        (2, 3, 4),
    ):
        result = _run(selected_train, args.start_balance, risk, daily_loss, daily_profit, consecutive)
        if result["max_closed_equity_dd_pct"] > 10.0 or result["traded_clusters"] < 30:
            continue
        score = result["return_pct"] - (2.0 * result["max_closed_equity_dd_pct"]) + (0.1 * result["positive_days"])
        candidates.append(
            {
                "score": round(score, 4),
                "risk_pct": risk,
                "daily_loss_pct": daily_loss,
                "daily_profit_pct": daily_profit,
                "max_consecutive_losses": consecutive,
                "train": result,
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    if not candidates:
        raise RuntimeError("No candidate passed the training constraints")
    winner = candidates[0]
    policy = {key: winner[key] for key in ("risk_pct", "daily_loss_pct", "daily_profit_pct", "max_consecutive_losses")}
    test_result = _run(selected_test, args.start_balance, **policy)
    full_result = _run(selected, args.start_balance, **policy)
    baseline = _run(all_events, args.start_balance, 1.5, 99.0, 99.0, 999)

    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "period": {"start": start.isoformat(), "split": split.isoformat(), "end": max(_parse_time(item) for item in all_events).isoformat()},
        "selection_rule": "train only: min sample, mean R > 0.03 and positive setups >= 50%",
        "selected": {"telegram_channels": telegram_selected, "scalpers": scalpers_selected},
        "policy": policy,
        "train_40d": winner["train"],
        "holdout_20d": test_result,
        "full_60d": full_result,
        "baseline_all_current_engines_1_5pct": baseline,
        "train_diagnostics": diagnostics,
        "holdout_diagnostics": holdout_diagnostics,
        "full_diagnostics": full_diagnostics,
        "top_training_candidates": candidates[:10],
        "assumptions": {
            "cluster_risk_cap": True,
            "max_win_r": 4.0,
            "max_loss_r": 1.05,
            "spread_price_per_leg": args.spread_price,
            "drawdown": "closed equity, not intrabar floating equity",
        },
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
