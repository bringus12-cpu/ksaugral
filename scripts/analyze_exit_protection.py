from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mt5_gateway import mt5


POLICIES = {
    "baseline": None,
    "be_at_050r": (0.50, 0.0, None),
    "be_at_075r": (0.75, 0.0, None),
    "be_at_100r": (1.00, 0.0, None),
    "lock_025r_at_075r": (0.75, 0.25, None),
    "lock_025r_at_100r": (1.00, 0.25, None),
    "trail_050r_after_100r": (1.00, None, 0.50),
    "trail_075r_after_100r": (1.00, None, 0.75),
}


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _r(side: str, entry: float, price: float, risk_distance: float) -> float:
    direction = 1.0 if side == "buy" else -1.0
    return direction * (price - entry) / risk_distance


def _prices(row: pd.Series, side: str, point: float) -> tuple[float, float, float]:
    spread = max(point, float(row.get("spread", 0.0) or 0.0) * point)
    if side == "sell":
        return float(row["high"]) + spread, float(row["low"]) + spread, float(row["close"]) + spread
    return float(row["high"]), float(row["low"]), float(row["close"])


def _path_result(event: dict[str, Any], bars: pd.DataFrame, point: float) -> dict[str, Any] | None:
    entry = float(event["entry"])
    sl = float(event["sl"])
    side = str(event["side"]).lower()
    distance = abs(entry - sl)
    if distance <= 0.0:
        return None
    opened, closed = _dt(event["opened"]), _dt(event["closed"])
    left = int(bars["time"].searchsorted(pd.Timestamp(opened), side="left"))
    right = int(bars["time"].searchsorted(pd.Timestamp(closed), side="right"))
    path = bars.iloc[left:right]
    if path.empty:
        return None

    baseline_r = _r(side, entry, float(event["exit"]), distance)
    favorable = 0.0
    adverse = 0.0
    policy_r = {name: baseline_r for name in POLICIES}
    active_stops: dict[str, float | None] = {name: None for name in POLICIES if name != "baseline"}
    finished: set[str] = set()
    for _, bar in path.iterrows():
        high, low, _close = _prices(bar, side, point)
        bar_favorable = _r(side, entry, high if side == "buy" else low, distance)
        bar_adverse = _r(side, entry, low if side == "buy" else high, distance)
        favorable = max(favorable, bar_favorable)
        adverse = min(adverse, bar_adverse)

        # Stops activated on a prior bar are checked before this bar can tighten them.
        for name, stop_r in active_stops.items():
            if name in finished or stop_r is None:
                continue
            if bar_adverse <= stop_r:
                policy_r[name] = stop_r
                finished.add(name)

        for name, specification in POLICIES.items():
            if name == "baseline" or name in finished or specification is None:
                continue
            trigger, fixed_lock, trail_distance = specification
            if favorable < trigger:
                continue
            candidate = fixed_lock if trail_distance is None else favorable - trail_distance
            current = active_stops[name]
            active_stops[name] = candidate if current is None else max(current, candidate)

    return {
        **event,
        "baseline_r": baseline_r,
        "mfe_r": favorable,
        "mae_r": adverse,
        "policy_r": policy_r,
    }


def _metrics(values: list[float]) -> dict[str, Any]:
    wins = [value for value in values if value > 0.0001]
    losses = [value for value in values if value < -0.0001]
    gross_loss = abs(sum(losses))
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "legs": len(values),
        "win_rate_pct": round(100.0 * len(wins) / max(1, len(wins) + len(losses)), 2),
        "total_r": round(sum(values), 3),
        "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_r": round(drawdown, 3),
    }


def _load_events(args: argparse.Namespace) -> list[dict[str, Any]]:
    start = _dt(args.start)
    events: list[dict[str, Any]] = []
    for label, raw_path in (("scalper_db60", args.db60), ("scalper_bbkelt", args.bbkelt)):
        report = json.loads(Path(raw_path).read_text(encoding="utf-8-sig"))
        for row in report.get("trades", []):
            opened = _dt(row["opened"])
            if opened < start:
                continue
            events.append(
                {
                    "module": label,
                    "opened": opened.isoformat(),
                    "closed": _dt(row["closed"]).isoformat(),
                    "side": row["side"],
                    "entry": float(row["entry"]),
                    "sl": float(row["initial_sl"]),
                    "exit": float(row["exit"]),
                }
            )
    machine = json.loads(Path(args.market_machine).read_text(encoding="utf-8-sig"))
    analytics = json.loads(Path(args.analytics).read_text(encoding="utf-8-sig"))
    selected = set(analytics.get("stable_pairs", []))
    for row in machine.get("trades", []):
        if f"{row['symbol']}::{row['strategy']}" not in selected:
            continue
        opened = _dt(row["entry_time"])
        if opened < start:
            continue
        events.append(
            {
                "module": f"market_machine:{row['strategy']}",
                "opened": opened.isoformat(),
                "closed": _dt(row["exit_time"]).isoformat(),
                "side": row["side"],
                "entry": float(row["entry"]),
                "sl": float(row["sl"]),
                "exit": float(row["exit"]),
            }
        )
    return sorted(events, key=lambda row: (row["opened"], row["closed"], row["module"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="M1 MAE/MFE and exit-protection comparison")
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--start", default="2026-06-22T00:00:00+00:00")
    parser.add_argument("--db60", required=True)
    parser.add_argument("--bbkelt", required=True)
    parser.add_argument("--market-machine", required=True)
    parser.add_argument("--analytics", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    env = dotenv_values(ROOT / args.env)
    assert mt5.initialize(path=env.get("MT5_PATH", env.get("MT5_TERMINAL_PATH", ""))), mt5.last_error()
    try:
        events = _load_events(args)
        start = min(_dt(row["opened"]) for row in events)
        end = max(_dt(row["closed"]) for row in events)
        raw = mt5.copy_rates_range("XAUUSD+", mt5.TIMEFRAME_M1, start, end)
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"Missing XAUUSD+ M1 rates: {mt5.last_error()}")
        bars = pd.DataFrame(raw)
        bars["time"] = pd.to_datetime(bars["time"], unit="s", utc=True)
        info = mt5.symbol_info("XAUUSD+")
        point = float(getattr(info, "point", 0.01) or 0.01)
        rows = [result for event in events if (result := _path_result(event, bars, point)) is not None]
    finally:
        mt5.shutdown()

    by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_module[row["module"]].append(row)

    chronological = sorted(rows, key=lambda row: (_dt(row["closed"]), _dt(row["opened"])))
    policy_totals = {
        policy: _metrics([float(row["policy_r"][policy]) for row in chronological])
        for policy in POLICIES
    }
    modules = {}
    recommendations: dict[str, str] = {}
    for module, items in sorted(by_module.items()):
        items = sorted(items, key=lambda row: (_dt(row["closed"]), _dt(row["opened"])))
        losses = [row for row in items if row["baseline_r"] < 0.0]
        module_policy_metrics = {
            policy: _metrics([float(row["policy_r"][policy]) for row in items])
            for policy in POLICIES
        }
        recommended = max(
            module_policy_metrics,
            key=lambda policy: float(module_policy_metrics[policy].get("total_r", -math.inf)),
        )
        recommendations[module] = recommended
        modules[module] = {
            "recommended_policy": recommended,
            "baseline": module_policy_metrics["baseline"],
            "policies": {
                policy: module_policy_metrics[policy]
                for policy in POLICIES if policy != "baseline"
            },
            "loss_diagnostics": {
                "losing_legs": len(losses),
                "wrong_from_start_mfe_below_025r": sum(row["mfe_r"] < 0.25 for row in losses),
                "gave_back_after_050r": sum(row["mfe_r"] >= 0.50 for row in losses),
                "gave_back_after_100r": sum(row["mfe_r"] >= 1.00 for row in losses),
            },
            "median_mfe_r": round(float(pd.Series([row["mfe_r"] for row in items]).median()), 3),
            "median_mae_r": round(float(pd.Series([row["mae_r"] for row in items]).median()), 3),
        }
    best = max(policy_totals, key=lambda key: float(policy_totals[key].get("total_r", -math.inf)))
    per_module_values = [
        float(row["policy_r"][recommendations[row["module"]]])
        for row in chronological
    ]
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "method": "M1 path replay; protection becomes active on the next minute; conservative stop-first check",
        "events_requested": len(events),
        "events_with_complete_path": len(rows),
        "best_global_policy": best,
        "portfolio_policies": policy_totals,
        "recommended_policy_by_module": recommendations,
        "per_module_recommended_portfolio": _metrics(per_module_values),
        "modules": modules,
        "limitations": [
            "Analysis covers DB60, BB/Keltner/MACD and the three newly selected Market Machine strategies.",
            "Telegram management messages are not replaced by synthetic protection in this comparison.",
            "M1 OHLC cannot reconstruct tick order inside a minute; new stops activate from the next bar.",
            "Policy selection is in-sample and must pass a forward test before production use.",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
