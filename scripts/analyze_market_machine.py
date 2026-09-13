from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd


def _drawdown(values: pd.Series) -> float:
    equity = values.cumsum()
    return float((equity.cummax() - equity).max()) if not equity.empty else 0.0


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    frame = pd.DataFrame(trades)
    frame["profit"] = pd.to_numeric(frame["profit"], errors="coerce").fillna(0.0)
    frame["exit_time"] = pd.to_datetime(frame["exit_time"], utc=True)
    daily = frame.groupby(frame["exit_time"].dt.date)["profit"].sum().sort_index()
    losses = frame.loc[frame["profit"] < 0.0, "profit"]
    wins = frame.loc[frame["profit"] > 0.0, "profit"]
    daily_std = float(daily.std(ddof=0))
    downside = float(daily.where(daily < 0.0, 0.0).std(ddof=0))
    gross_loss = abs(float(losses.sum()))
    return {
        "trades": int(len(frame)),
        "trading_days": int(len(daily)),
        "pnl": round(float(frame["profit"].sum()), 2),
        "win_rate_pct": round(100.0 * float((frame["profit"] > 0.0).mean()), 2),
        "profit_factor": round(float(wins.sum()) / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown": round(_drawdown(frame.sort_values("exit_time")["profit"]), 2),
        "daily_sharpe_like": round(math.sqrt(252.0) * float(daily.mean()) / daily_std, 3) if daily_std else 0.0,
        "daily_sortino_like": round(math.sqrt(252.0) * float(daily.mean()) / downside, 3) if downside else 0.0,
        "daily_var_95": round(float(daily.quantile(0.05)), 2),
        "daily_cvar_95": round(float(daily[daily <= daily.quantile(0.05)].mean()), 2),
        "positive_days_pct": round(100.0 * float((daily > 0.0).mean()), 2),
    }


def _walk_forward(trades: list[dict], folds: int = 4) -> dict:
    ordered = sorted(trades, key=lambda item: item["exit_time"])
    chunks = np.array_split(np.arange(len(ordered)), folds)
    results = [_metrics([ordered[int(index)] for index in chunk]) for chunk in chunks if len(chunk)]
    return {
        "folds": results,
        "positive_folds": sum(float(fold.get("pnl", 0.0)) > 0.0 for fold in results),
        "stable": len(results) == folds and sum(float(fold.get("pnl", 0.0)) > 0.0 for fold in results) >= folds - 1,
    }


def _bootstrap(daily: pd.Series, starting_balance: float, paths: int = 2000, seed: int = 42) -> dict:
    if daily.empty:
        return {}
    generator = np.random.default_rng(seed)
    sampled_days = generator.choice(daily.to_numpy(dtype=float), size=(paths, len(daily)), replace=True)
    samples = sampled_days.sum(axis=1)
    equity_paths = starting_balance + sampled_days.cumsum(axis=1)
    peaks = np.maximum.accumulate(np.column_stack([np.full(paths, starting_balance), equity_paths]), axis=1)[:, 1:]
    max_drawdowns = (peaks - equity_paths).max(axis=1)
    return {
        "paths": paths,
        "probability_of_loss_pct": round(100.0 * float((samples < 0.0).mean()), 2),
        "p05": round(float(np.quantile(samples, 0.05)), 2),
        "median": round(float(np.median(samples)), 2),
        "p95": round(float(np.quantile(samples, 0.95)), 2),
        "median_max_drawdown": round(float(np.median(max_drawdowns)), 2),
        "p95_max_drawdown": round(float(np.quantile(max_drawdowns, 0.95)), 2),
        "probability_50pct_drawdown_pct": round(100.0 * float((max_drawdowns >= starting_balance * 0.50).mean()), 2),
        "probability_ruin_pct": round(100.0 * float((equity_paths.min(axis=1) <= 0.0).mean()), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Robustness and portfolio analytics for Market Machine")
    parser.add_argument("report")
    parser.add_argument("--output", default="data_vantage/market_machine_analytics.json")
    parser.add_argument("--starting-balance", type=float, default=1000.0)
    args = parser.parse_args()

    source = json.loads(Path(args.report).read_text(encoding="utf-8"))
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in source.get("trades", []):
        grouped[f"{trade['symbol']}::{trade['strategy']}"].append(trade)

    pair_analytics = {}
    daily_columns = {}
    for key, trades in grouped.items():
        metrics = _metrics(trades)
        walk_forward = _walk_forward(trades)
        frame = pd.DataFrame(trades)
        frame["exit_time"] = pd.to_datetime(frame["exit_time"], utc=True)
        frame["profit"] = pd.to_numeric(frame["profit"], errors="coerce").fillna(0.0)
        daily = frame.groupby(frame["exit_time"].dt.date)["profit"].sum().sort_index()
        pair_analytics[key] = {
            **metrics,
            "walk_forward": walk_forward,
            "bootstrap": _bootstrap(daily, args.starting_balance),
        }
        daily_columns[key] = daily

    stable_pairs = [
        key
        for key, stats in pair_analytics.items()
        if stats.get("trades", 0) >= 40
        and stats.get("trading_days", 0) >= 20
        and stats.get("pnl", 0.0) > 0.0
        and (stats.get("profit_factor") or 0.0) >= 1.05
        and stats["walk_forward"]["stable"]
        and stats["bootstrap"].get("probability_of_loss_pct", 100.0) <= 35.0
    ]

    correlations = []
    daily_frame = pd.DataFrame()
    if daily_columns:
        daily_frame = pd.DataFrame(daily_columns).fillna(0.0)
        matrix = daily_frame.corr()
        for index, left in enumerate(matrix.columns):
            for right in matrix.columns[index + 1 :]:
                value = float(matrix.loc[left, right])
                if math.isfinite(value):
                    correlations.append({"left": left, "right": right, "correlation": round(value, 3)})
    correlations.sort(key=lambda item: abs(item["correlation"]), reverse=True)

    diversified_pairs: list[str] = []
    ranked_pairs = sorted(
        stable_pairs,
        key=lambda key: (
            pair_analytics[key].get("daily_sharpe_like", 0.0),
            pair_analytics[key].get("profit_factor") or 0.0,
            pair_analytics[key].get("pnl", 0.0),
        ),
        reverse=True,
    )
    for candidate in ranked_pairs:
        if len(diversified_pairs) >= 12:
            break
        if all(
            abs(float(daily_frame.loc[:, candidate].corr(daily_frame.loc[:, selected]))) <= 0.80
            for selected in diversified_pairs
        ):
            diversified_pairs.append(candidate)

    selected_trades = [
        trade
        for trade in source.get("trades", [])
        if f"{trade['symbol']}::{trade['strategy']}" in set(stable_pairs)
    ]
    diversified_trades = [
        trade
        for trade in source.get("trades", [])
        if f"{trade['symbol']}::{trade['strategy']}" in set(diversified_pairs)
    ]
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source": str(Path(args.report).resolve()),
        "method": "per-pair metrics, four chronological folds, daily bootstrap and return correlation",
        "stable_pairs": sorted(stable_pairs),
        "stable_portfolio": _metrics(selected_trades),
        "diversified_pairs": diversified_pairs,
        "diversified_portfolio": _metrics(diversified_trades),
        "pairs": pair_analytics,
        "top_absolute_correlations": correlations[:50],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"pairs", "top_absolute_correlations"}}, indent=2, ensure_ascii=False))
    print(f"REPORT={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
