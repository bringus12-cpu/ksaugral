"""Offline component attribution; deliberately not a broker-equity backtest."""
from __future__ import annotations

import heapq
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.merge_selected_channels_portfolio import _base_events, _candidate_events, _is_mirror


def load(name):
    return json.loads((ROOT / "data_vantage" / name).read_text(encoding="utf-8"))


def metrics(events):
    pnls = [e["pnl_per_lot"] * .01 for e in events]
    positive = sum(max(0, p) for p in pnls)
    negative = -sum(min(0, p) for p in pnls)
    return dict(legs=len(events), pnl_001=round(sum(pnls), 2),
                win_rate=round(100 * sum(p > .005 for p in pnls) / max(1, len(pnls)), 2),
                profit_factor=round(positive / negative, 3) if negative else None)


def replay(events, drag=0.0):
    balance = peak = 700.0
    dd = 0.0
    active = []
    seq = 0
    max_open = 0
    def close(moment):
        nonlocal balance, peak, dd
        while active and active[0][0] <= moment:
            _, _, pnl = heapq.heappop(active)
            balance += pnl
            peak = max(peak, balance)
            dd = max(dd, 100 * (peak - balance) / peak)
    for e in sorted(events, key=lambda e: (e["opened"], e["closed"])):
        close(e["opened"])
        risk = .0175 * max(0, balance)
        pnl = risk * (e["pnl_per_lot"] / e["loss_per_lot"] - drag)
        heapq.heappush(active, (e["closed"], seq, pnl))
        seq += 1
        max_open = max(max_open, len(active))
    close(datetime.max.replace(tzinfo=UTC))
    return dict(final_balance=round(balance, 2), closed_dd_pct=round(dd, 2), max_concurrent=max_open)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channels-report", default="selected9_channels_70sessions_events_20260912.json")
    parser.add_argument("--output-name", default="bot_component_audit_20260913.json")
    args = parser.parse_args()
    base = _base_events(load("base_all_modules_start700_risk175_70sessions_20260912.json"))
    new_report = load(args.channels_report)
    new = _candidate_events(new_report)
    env = dotenv_values(ROOT / ".env.vantage")
    watched = set(env["TRADE_CHANNELS"].split(","))
    excluded = Counter()
    eligible = []
    for e in new:
        name = e["module"].split(":", 1)[1]
        if name not in watched:
            excluded["not_currently_traded"] += 1
        elif not e["symbol"].upper().startswith("XAUUSD"):
            excluded["non_gold_missing_contract_specification"] += 1
        else:
            # Diagnostic XAU standard contract only, not a substitute for broker metadata.
            e["loss_per_lot"] = abs(e["entry"] - e["sl"]) * 100
            eligible.append(e)
    kept = list(base)
    removed = Counter()
    for e in eligible:
        previous = sorted([x for x in kept if not x["module"].startswith("scalper")], key=lambda x: x["opened"])
        if _is_mirror(e, previous):
            removed[e["module"]] += 1
        else:
            kept.append(e)
    groups = defaultdict(list)
    for e in kept:
        groups[e["module"]].append(e)
    split = datetime(2026, 8, 3, tzinfo=UTC)
    attribution = {name: {"all": metrics(rows), "early": metrics([e for e in rows if e["opened"] < split]),
                          "late": metrics([e for e in rows if e["opened"] >= split])}
                   for name, rows in sorted(groups.items())}
    variants = {
        "current_available": kept,
        "without_db60": [e for e in kept if e["module"] != "scalper_db60"],
        "without_extra_phoenix_tp5_tp6": [e for e in kept if e["module"] != "phoenix_profit"],
        "without_phoenix_range": [e for e in kept if e["module"] != "phoenix_range"],
        "without_new_channels": base,
        "new_channels_tp1_only": [e for e in kept if not e["module"].startswith("newtg:") or e["target_index"] == 1],
    }
    comparison = {name: {"fixed_001": metrics(rows), "closed_balance_175": replay(rows),
                         "extra_cost_003R": replay(rows, .03),
                         "late_only_start700": replay([e for e in rows if e["opened"] >= split])}
                  for name, rows in variants.items()}
    weekly = {}
    for folder in ("data_vantage", "data_puprime_live"):
        p = json.loads((ROOT / folder / "weekly_live_20260907_20260911.json").read_text())
        weekly[folder] = {key: p[key] for key in ("generated_utc", "period", "total", "by_engine", "by_channel")}
    report = dict(generated_utc=datetime.now(UTC).isoformat(), channels_report=args.channels_report, weekly=weekly, by_module=attribution,
                  comparisons=comparison, duplicates_removed=dict(removed), exclusions=dict(excluded),
                  source_new_legs=sum(len(c.get("trade_events", [])) for c in new_report["channels"]),
                  parsed_new_legs=len(new), forecast_eligible=False,
                  limitations=["Not an exact replay of current deployed strategy. Existing source trades reused.",
                               "Sizing uses closed balance, fractional lots, no margin or stopout. Not equity sizing.",
                               "Dany source absent from base portfolio despite active channel configuration.",
                               "Legacy new-channel report uses future bar close. Causal report uses next available bar open.",
                               "Final edited Telegram text and incomplete floating bar coverage invalidate live forecasts.",
                               "Selection uses the same historical period; late split is diagnostic, not untouched validation."])
    path = ROOT / "reports" / args.output_name
    path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(dict(report=str(path), comparisons=comparison, by_module=attribution, exclusions=dict(excluded),
                          duplicates_removed=dict(removed)), indent=2))


if __name__ == "__main__":
    main()
