from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _minute(raw: str) -> str:
    return str(raw)[:16]


def _bounded_r(value: float, max_win_r: float, max_loss_r: float) -> float:
    return min(max_win_r, max(-max_loss_r, float(value)))


def _scalper_events(path: Path, label: str, max_win_r: float, max_loss_r: float) -> list[dict[str, Any]]:
    report = _read(path)
    settings = report.get("settings", {})
    sl_distance = float(settings.get("sl", 0.0) or 0.0)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in report.get("trades", []):
        grouped[str(trade.get("opened", ""))].append(trade)

    events: list[dict[str, Any]] = []
    for opened, legs in grouped.items():
        net_001 = sum(float(item.get("profit_001", 0.0) or 0.0) - float(item.get("spread_cost", 0.0) or 0.0) for item in legs)
        spread_risk = sum(float(item.get("spread_cost", 0.0) or 0.0) for item in legs)
        risk_001 = (sl_distance * len(legs)) + spread_risk
        if risk_001 <= 0:
            continue
        events.append(
            {
                "opened": opened,
                "cluster": _minute(opened),
                "source": "scalper",
                "strategy": label,
                "legs": len(legs),
                "r": _bounded_r(net_001 / risk_001, max_win_r, max_loss_r),
            }
        )
    return events


def _telegram_events(path: Path, spread_price: float, max_win_r: float, max_loss_r: float) -> list[dict[str, Any]]:
    report = _read(path)
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for leg in report.get("equity", []):
        key = (str(leg.get("signal_time", "")), str(leg.get("channel", "")), int(leg.get("message_id", 0) or 0))
        grouped[key].append(leg)

    events: list[dict[str, Any]] = []
    for (signal_time, channel, _message_id), legs in grouped.items():
        net_001 = sum(float(item.get("profit_001", 0.0) or 0.0) - spread_price for item in legs)
        risk_001 = sum(abs(float(item.get("entry", 0.0) or 0.0) - float(item.get("initial_sl", 0.0) or 0.0)) + spread_price for item in legs)
        if risk_001 <= 0:
            continue
        events.append(
            {
                "opened": signal_time,
                "cluster": _minute(signal_time),
                "source": "telegram",
                "strategy": channel,
                "legs": len(legs),
                "side": str(legs[0].get("side", "")),
                "entry": round(mean(float(item.get("entry", 0.0) or 0.0) for item in legs), 1),
                "initial_sl": round(mean(float(item.get("initial_sl", 0.0) or 0.0) for item in legs), 1),
                "r": _bounded_r(net_001 / risk_001, max_win_r, max_loss_r),
            }
        )
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float, float]] = set()
    for event in sorted(events, key=lambda item: item["opened"]):
        opened = datetime.fromisoformat(str(event["opened"]).replace("Z", "+00:00"))
        bucket_minute = (opened.minute // 5) * 5
        bucket = opened.replace(minute=bucket_minute, second=0, microsecond=0).isoformat()
        fingerprint = (bucket, str(event["side"]), float(event["entry"]), float(event["initial_sl"]))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduplicated.append(event)
    return deduplicated


def _cluster_returns(events: list[dict[str, Any]], cap_cluster_risk: bool) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["cluster"]].append(event)
    rows: list[dict[str, Any]] = []
    for cluster, items in grouped.items():
        r_value = mean(float(item["r"]) for item in items) if cap_cluster_risk else sum(float(item["r"]) for item in items)
        rows.append({"time": cluster, "date": cluster[:10], "r": r_value, "setups": len(items), "legs": sum(int(item["legs"]) for item in items)})
    return sorted(rows, key=lambda item: item["time"])


def _simulate(events: list[dict[str, Any]], start_balance: float, risk_pct: float, cap_cluster_risk: bool) -> dict[str, Any]:
    clusters = _cluster_returns(events, cap_cluster_risk)
    balance = float(start_balance)
    peak = balance
    max_dd_pct = 0.0
    wins = 0
    losses = 0
    daily_r: dict[str, float] = defaultdict(float)
    for row in clusters:
        r_value = float(row["r"])
        balance += balance * risk_pct / 100.0 * r_value
        peak = max(peak, balance)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - balance) / peak * 100.0)
        wins += r_value > 0
        losses += r_value < 0
        daily_r[row["date"]] += r_value
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
        "max_closed_equity_drawdown_pct": round(max_dd_pct, 2),
        "clusters": len(clusters),
        "setups": len(events),
        "legs": sum(int(item["legs"]) for item in events),
        "positive_clusters": wins,
        "negative_clusters": losses,
        "positive_cluster_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "daily_r": dict(sorted(daily_r.items())),
    }


def _business_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))]


def _forecast(
    daily_r: dict[str, float],
    start_balance: float,
    risk_pct: float,
    end_date: date,
    paths: int,
    seed: int,
    positive_edge_haircut: float,
) -> dict[str, Any]:
    samples = [value * positive_edge_haircut if value > 0 else value for value in daily_r.values()]
    future_days = _business_days(date.today() + timedelta(days=1), end_date)
    if not samples or not future_days:
        return {}
    rng = random.Random(seed)
    month_ends = sorted({day.strftime("%Y-%m") for day in future_days})
    month_values: dict[str, list[float]] = {month: [] for month in month_ends}
    final_values: list[float] = []
    ruin_count = 0
    for _ in range(paths):
        balance = float(start_balance)
        last_month = ""
        for day in future_days:
            month = day.strftime("%Y-%m")
            if last_month and month != last_month:
                month_values[last_month].append(balance)
            daily_value = rng.choice(samples)
            balance *= 1.0 + (risk_pct / 100.0 * daily_value)
            balance = max(0.0, balance)
            last_month = month
            if balance <= 0:
                ruin_count += 1
                break
        if last_month:
            month_values[last_month].append(balance)
        final_values.append(balance)
    return {
        "start_date": future_days[0].isoformat(),
        "end_date": future_days[-1].isoformat(),
        "trading_days": len(future_days),
        "paths": paths,
        "method": "bootstrap of historical daily R with replacement",
        "positive_daily_edge_haircut": positive_edge_haircut,
        "ruin_probability_pct": round(100.0 * ruin_count / paths, 2),
        "end_balance": {
            "p10": round(_quantile(final_values, 0.10), 2),
            "median": round(_quantile(final_values, 0.50), 2),
            "p90": round(_quantile(final_values, 0.90), 2),
        },
        "month_end_median": {month: round(_quantile(values, 0.50), 2) for month, values in month_values.items() if values},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=1.5)
    parser.add_argument("--telegram", required=True)
    parser.add_argument("--scalper", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--spread-price", type=float, default=0.18)
    parser.add_argument("--max-win-r", type=float, default=4.0)
    parser.add_argument("--max-loss-r", type=float, default=1.05)
    parser.add_argument("--forecast-positive-haircut", type=float, default=0.5)
    parser.add_argument("--forecast-end", default=f"{date.today().year}-12-31")
    parser.add_argument("--paths", type=int, default=10000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    telegram = _telegram_events(Path(args.telegram), float(args.spread_price), args.max_win_r, args.max_loss_r)
    scalpers: list[dict[str, Any]] = []
    by_strategy: dict[str, dict[str, Any]] = {}
    for raw in args.scalper:
        label, path = raw.split("=", 1)
        events = _scalper_events(Path(path), label, args.max_win_r, args.max_loss_r)
        scalpers.extend(events)
        by_strategy[label] = _simulate(events, args.start_balance, args.risk_pct, False)

    telegram_result = _simulate(telegram, args.start_balance, args.risk_pct, False)
    scalper_literal = _simulate(scalpers, args.start_balance, args.risk_pct, False)
    combined = telegram + scalpers
    literal = _simulate(combined, args.start_balance, args.risk_pct, False)
    capped = _simulate(combined, args.start_balance, args.risk_pct, True)
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "assumptions": {
            "risk_per_setup_pct": args.risk_pct,
            "risk_split_equally_across_legs": True,
            "spread_price_per_leg": args.spread_price,
            "starting_balance": args.start_balance,
            "drawdown_basis": "closed equity; intrabar floating drawdown unavailable",
            "telegram_deduplication": "5-minute bucket plus side, rounded entry and initial SL",
            "bounded_setup_r": {"max_win_r": args.max_win_r, "max_loss_r": args.max_loss_r},
        },
        "historical_60d": {
            "telegram_only": telegram_result,
            "scalpers_only_literal": scalper_literal,
            "combined_literal_1_5_each_engine": literal,
            "combined_cluster_cap_1_5": capped,
            "scalper_by_strategy": by_strategy,
        },
        "forecast_to_year_end": {
            "literal": _forecast(literal["daily_r"], args.start_balance, args.risk_pct, date.fromisoformat(args.forecast_end), args.paths, 20260714, args.forecast_positive_haircut),
            "cluster_capped": _forecast(capped["daily_r"], args.start_balance, args.risk_pct, date.fromisoformat(args.forecast_end), args.paths, 20260715, args.forecast_positive_haircut),
        },
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
