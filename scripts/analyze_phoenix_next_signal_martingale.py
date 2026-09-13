from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _signal_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"message_id": 0, "signal_time": "", "pnl": 0.0, "legs": 0}
    )
    for trade in report.get("trades", []):
        message_id = int(trade["message_id"])
        row = grouped[message_id]
        row["message_id"] = message_id
        row["signal_time"] = str(trade["signal_time"])
        row["pnl"] += float(trade.get("pnl_001", 0.0) or 0.0)
        row["legs"] += 1
    return sorted(grouped.values(), key=lambda row: (row["signal_time"], row["message_id"]))


def _max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 2)


def _simulate(rows: list[dict[str, Any]], threshold: float, multiplier: float) -> dict[str, Any]:
    adjusted: list[float] = []
    events: list[dict[str, Any]] = []
    arm_next = False
    source: dict[str, Any] | None = None
    recovered = 0
    made_worse = 0

    for row in rows:
        factor = multiplier if arm_next else 1.0
        pnl = float(row["pnl"])
        adjusted_pnl = pnl * factor
        adjusted.append(adjusted_pnl)
        if arm_next and source is not None:
            recovered_now = adjusted_pnl > 0 and adjusted_pnl + float(source["pnl"]) >= 0
            recovered += int(recovered_now)
            made_worse += int(adjusted_pnl < 0)
            events.append(
                {
                    "loss_message_id": source["message_id"],
                    "loss_time": source["signal_time"],
                    "loss_pnl_001": round(float(source["pnl"]), 2),
                    "next_message_id": row["message_id"],
                    "next_time": row["signal_time"],
                    "next_base_pnl_001": round(pnl, 2),
                    "next_adjusted_pnl_001": round(adjusted_pnl, 2),
                    "recovered_previous_loss": recovered_now,
                }
            )
        arm_next = pnl <= threshold
        source = row if arm_next else None

    baseline = [float(row["pnl"]) for row in rows]
    return {
        "loss_threshold_pnl_001": threshold,
        "next_signal_multiplier": multiplier,
        "signals": len(rows),
        "triggered_recovery_signals": len(events),
        "recovered_previous_loss_count": recovered,
        "recovery_rate_pct": round(recovered / len(events) * 100.0, 2) if events else 0.0,
        "next_signal_loss_count": made_worse,
        "next_signal_loss_rate_pct": round(made_worse / len(events) * 100.0, 2) if events else 0.0,
        "baseline_pnl_001": round(sum(baseline), 2),
        "adjusted_pnl_001": round(sum(adjusted), 2),
        "pnl_change_001": round(sum(adjusted) - sum(baseline), 2),
        "baseline_max_closed_drawdown_001": _max_drawdown(baseline),
        "adjusted_max_closed_drawdown_001": _max_drawdown(adjusted),
        "events": events,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--output", default="data_vantage/phoenix_next_signal_martingale_analysis.json")
    args = parser.parse_args()

    result: dict[str, Any] = {"method": "one-step multiplier on the next executed full Phoenix signal only", "reports": []}
    for raw_path in args.reports:
        path = Path(raw_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        rows = _signal_rows(report)
        variants = [
            _simulate(rows, threshold, multiplier)
            for threshold in (-5.0, -10.0, -20.0)
            for multiplier in (1.25, 1.5, 2.0)
        ]
        result["reports"].append(
            {
                "source": str(path),
                "range_utc": report.get("range_utc"),
                "signal_count": len(rows),
                "baseline": {
                    "pnl_001": round(sum(float(row["pnl"]) for row in rows), 2),
                    "max_closed_drawdown_001": _max_drawdown([float(row["pnl"]) for row in rows]),
                },
                "variants": variants,
            }
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    compact = {
        "output": str(output),
        "reports": [
            {
                "source": row["source"],
                "baseline": row["baseline"],
                "variants": [
                    {key: value for key, value in variant.items() if key != "events"}
                    for variant in row["variants"]
                ],
            }
            for row in result["reports"]
        ],
    }
    print(json.dumps(compact, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
