from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.history_cache import load_cached_rates


def _summary(values: list[float]) -> dict[str, float | int | None]:
    wins = [value for value in values if value > 0.005]
    losses = [value for value in values if value < -0.005]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(values),
        "win_rate_pct": round(100.0 * len(wins) / max(1, len(wins) + len(losses)), 2),
        "pnl_001_per_leg": round(sum(values), 2),
        "pnl_three_legs": round(3.0 * sum(values), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


def _batches(report: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    sessions = [str(value)[:10] for value in report["sessions_included"]]
    block_by_day = {day: index // 60 + 1 for index, day in enumerate(sessions)}
    for row in report["trades"]:
        key = (str(row["opened"]), str(row["setup_tag"]), str(row["side"]))
        if key in seen:
            continue
        seen.add(key)
        opened = pd.Timestamp(row["opened"])
        output.append({
            "opened": opened,
            "day": opened.date().isoformat(),
            "block": block_by_day.get(opened.date().isoformat(), 0),
            "hour": int(opened.hour),
            "side": str(row["side"]),
            "setup": str(row["setup_tag"]),
            "entry": float(row["entry"]),
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--point", type=float, default=0.01)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8-sig"))
    batches = _batches(report)
    rates = load_cached_rates(args.history, "M1")
    rates["time"] = pd.to_datetime(rates["time"], utc=True)
    times = rates["time"].to_numpy(dtype="datetime64[ns]")
    highs = rates["high"].to_numpy(dtype=float)
    lows = rates["low"].to_numpy(dtype=float)
    closes = rates["close"].to_numpy(dtype=float)
    spreads = rates["spread"].to_numpy(dtype=float) * float(args.point)

    prepared: list[dict[str, Any]] = []
    for batch in batches:
        start = int(np.searchsorted(times, np.datetime64(batch["opened"].to_datetime64()), side="left"))
        end = min(len(rates), start + 24 * 60)
        if start >= len(rates) or end <= start:
            continue
        prepared.append({**batch, "start": start, "end": end, "spread": float(spreads[start])})

    universes = {
        "all_current_triggers": lambda row: True,
        "adx_07": lambda row: row["setup"] == "SC-AdxBreak" and row["hour"] == 7,
    }
    candidates: list[dict[str, Any]] = []
    for universe_name, selector in universes.items():
        selected = [row for row in prepared if selector(row)]
        for tp in (1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
            for sl in (2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0):
                for be_trigger in (0.0, 0.75, 1.0, 1.25, 1.5, 2.0):
                    if be_trigger >= tp:
                        continue
                    values: list[float] = []
                    blocks: dict[int, list[float]] = {index: [] for index in range(1, 6)}
                    for row in selected:
                        entry = float(row["entry"])
                        side = str(row["side"])
                        stop = entry - sl if side == "buy" else entry + sl
                        target = entry + tp if side == "buy" else entry - tp
                        exit_price = float(closes[row["end"] - 1])
                        for index in range(int(row["start"]), int(row["end"])):
                            high = float(highs[index])
                            low = float(lows[index])
                            favorable = high - entry if side == "buy" else entry - low
                            if be_trigger > 0.0 and favorable >= be_trigger:
                                candidate = entry + 0.15 if side == "buy" else entry - 0.15
                                stop = max(stop, candidate) if side == "buy" else min(stop, candidate)
                            hit_tp = high >= target if side == "buy" else low <= target
                            hit_sl = low <= stop if side == "buy" else high >= stop
                            if hit_tp:
                                exit_price = target
                                break
                            if hit_sl:
                                exit_price = stop
                                break
                        move = exit_price - entry if side == "buy" else entry - exit_price
                        pnl = move - float(row["spread"]) - float(args.commission_per_001)
                        values.append(pnl)
                        if int(row["block"]) in blocks:
                            blocks[int(row["block"])].append(pnl)
                    block_summaries = [_summary(blocks[index]) for index in range(1, 6)]
                    dev_values = [value for index in range(3) for value in blocks[index + 1]]
                    holdout_values = [value for index in range(3, 5) for value in blocks[index + 1]]
                    candidates.append({
                        "universe": universe_name,
                        "tp_usd": tp,
                        "sl_usd": sl,
                        "be_trigger_usd": be_trigger,
                        "be_buffer_usd": 0.15 if be_trigger > 0 else 0.0,
                        "full": _summary(values),
                        "development_first_180": _summary(dev_values),
                        "holdout_last_120": _summary(holdout_values),
                        "blocks_60": block_summaries,
                        "positive_blocks": sum(float(item["pnl_three_legs"]) > 0 for item in block_summaries),
                    })

    qualified = [
        row for row in candidates
        if float(row["development_first_180"]["pnl_three_legs"]) > 0
        and float(row["holdout_last_120"]["pnl_three_legs"]) > 0
        and int(row["positive_blocks"]) >= 4
        and float(row["full"].get("profit_factor") or 0.0) > 1.05
    ]
    qualified.sort(key=lambda row: (
        int(row["positive_blocks"]),
        float(row["holdout_last_120"]["pnl_three_legs"]),
        float(row["full"]["pnl_three_legs"]),
    ), reverse=True)
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source_report": args.report,
        "batches": len(prepared),
        "method": "Exit-grid replay over the exact current trigger stream; fixed 0.01 per leg, three identical legs, dynamic historical spread and commission; first 180 sessions development and last 120 holdout.",
        "qualified_count": len(qualified),
        "best_qualified": qualified[:20],
        "all_candidates": candidates,
        "limitation": "Filtering or longer exits can suppress later signals in live sequential execution; this replay keeps the original trigger timestamps and is therefore an exit-quality diagnostic, not a standalone production estimate.",
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"batches": len(prepared), "qualified": len(qualified), "best": qualified[:5]}, indent=2))


if __name__ == "__main__":
    main()
