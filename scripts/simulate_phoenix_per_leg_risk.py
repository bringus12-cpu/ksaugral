from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=None)


def _load_trades(path: Path, module: str, *, exclude_market: bool = False) -> list[dict[str, Any]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for raw in report.get("trades", []):
        if exclude_market and str(raw.get("fill_kind", "")).lower() == "market":
            continue
        entry = float(raw.get("entry", 0.0) or 0.0)
        sl = float(raw.get("sl", 0.0) or 0.0)
        if entry <= 0.0 or sl <= 0.0 or abs(entry - sl) < 0.01:
            continue
        rows.append(
            {
                "module": module,
                "message_id": int(raw.get("message_id", 0) or 0),
                "entry_time": _time(raw["entry_time"]),
                "exit_time": _time(raw["exit_time"]),
                "entry": entry,
                "sl": sl,
                "pnl_001": float(raw.get("pnl_001", 0.0) or 0.0),
                "status": str(raw.get("status", "")),
                "target_index": int(raw.get("target_index", raw.get("leg", 0)) or 0),
            }
        )
    return rows


def simulate(
    trades: list[dict[str, Any]],
    start_balance: float,
    risk_pct_per_leg: float,
    commission_per_001: float,
) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_drawdown = 0.0
    ruined = False
    active: list[tuple[datetime, int, dict[str, Any]]] = []
    sequence = 0
    completed: list[dict[str, Any]] = []
    max_concurrent_legs = 0
    max_allocated_risk_usd = 0.0

    def close_due(moment: datetime) -> None:
        nonlocal balance, peak, max_drawdown
        while active and active[0][0] <= moment:
            _, _, item = heapq.heappop(active)
            balance += float(item["pnl"])
            peak = max(peak, balance)
            max_drawdown = min(max_drawdown, balance - peak)
            item["balance_after"] = round(balance, 2)
            completed.append(item)

    for row in sorted(trades, key=lambda item: (item["entry_time"], item["exit_time"])):
        close_due(row["entry_time"])
        if balance <= 0.0:
            ruined = True
            break
        risk_usd = balance * max(0.0, risk_pct_per_leg) / 100.0
        loss_at_001 = abs(float(row["entry"]) - float(row["sl"])) + max(0.0, commission_per_001)
        units_001 = max(1, int(math.floor(risk_usd / max(0.01, loss_at_001))))
        lot = round(units_001 * 0.01, 2)
        pnl = float(row["pnl_001"]) * units_001
        item = {
            **row,
            "lot": lot,
            "risk_usd": round(loss_at_001 * units_001, 2),
            "pnl": round(pnl, 2),
            "balance_at_entry": round(balance, 2),
        }
        sequence += 1
        heapq.heappush(active, (row["exit_time"], sequence, item))
        max_concurrent_legs = max(max_concurrent_legs, len(active))
        max_allocated_risk_usd = max(
            max_allocated_risk_usd,
            sum(float(open_item[2]["risk_usd"]) for open_item in active),
        )

    close_due(datetime.max)
    by_module: dict[str, dict[str, Any]] = {}
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in completed:
        groups[str(row["module"])].append(row)
    for module, rows in sorted(groups.items()):
        wins = sum(float(row["pnl"]) > 0.0 for row in rows)
        losses = sum(float(row["pnl"]) < 0.0 for row in rows)
        gross_win = sum(max(0.0, float(row["pnl"])) for row in rows)
        gross_loss = abs(sum(min(0.0, float(row["pnl"])) for row in rows))
        by_module[module] = {
            "positions": len(rows),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
            "pnl": round(sum(float(row["pnl"]) for row in rows), 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
            "status": dict(Counter(str(row["status"]) for row in rows)),
        }
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "pnl": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "max_closed_drawdown_usd": round(max_drawdown, 2),
        "max_closed_drawdown_pct_of_start": round(100.0 * abs(max_drawdown) / start_balance, 2),
        "risk_pct_per_leg": risk_pct_per_leg,
        "max_concurrent_legs": max_concurrent_legs,
        "max_allocated_risk_usd": round(max_allocated_risk_usd, 2),
        "ruined": ruined,
        "by_module": by_module,
        "trades": completed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", required=True)
    parser.add_argument("--range", dest="range_report", required=True)
    parser.add_argument("--direction", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct-per-leg", type=float, default=1.5)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--include-full-market", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    trades = [
        *_load_trades(Path(args.full), "phoenix_full", exclude_market=not args.include_full_market),
        *_load_trades(Path(args.range_report), "phoenix_range"),
        *_load_trades(Path(args.direction), "phoenix_direction"),
    ]
    report = simulate(
        trades,
        float(args.start_balance),
        float(args.risk_pct_per_leg),
        float(args.commission_per_001),
    )
    report["inputs"] = {
        "full": args.full,
        "range": args.range_report,
        "direction": args.direction,
        "include_full_market": bool(args.include_full_market),
        "commission_per_001": float(args.commission_per_001),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "trades"}, indent=2))


if __name__ == "__main__":
    main()
