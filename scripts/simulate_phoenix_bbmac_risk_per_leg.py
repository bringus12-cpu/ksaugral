from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _normal(symbol: str, cfg: Any, lot: float) -> float:
    return normalize_volume(symbol, lot, float(cfg.min_lot), max(999.0, float(cfg.max_lot)))


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _phoenix_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in report.get("trades", []):
        old_lot = max(0.01, float(row.get("lot", 0.01) or 0.01))
        risk = max(0.01, float(row.get("risk_usd", 0.0) or 0.0))
        events.append(
            {
                "module": str(row.get("module", "phoenix")),
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row.get("side", "buy")),
                "entry": float(row["entry"]),
                "loss_per_lot": risk / old_lot,
                "pnl_per_lot": float(row.get("pnl", 0.0) or 0.0) / old_lot,
                "status": str(row.get("status", "")),
            }
        )
    return events


def _bbmac_events(report: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in report.get("trades", []):
        opened = _dt(row["opened"])
        if not (start <= opened < end):
            continue
        old_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        old_pnl = float(row.get("profit", 0.0) or 0.0)
        reference_balance = max(0.01, float(row.get("balance", 0.0) or 0.0) - old_pnl)
        # The source replay sized this exact trade at 1.5%. Recovering the
        # implied loss per lot preserves its ATR-based initial stop.
        loss_per_lot = reference_balance * 0.015 / old_lot
        events.append(
            {
                "module": "scalper_bbmac",
                "opened": opened,
                "closed": _dt(row["closed"]),
                "side": str(row.get("side", "buy")),
                "entry": float(row["entry"]),
                "loss_per_lot": loss_per_lot,
                "pnl_per_lot": old_pnl / old_lot,
                "status": str(row.get("status", "")),
            }
        )
    return events


def _simulate(events: list[dict[str, Any]], symbol: str, cfg: Any, start_balance: float, risk_pct: float, enforce_margin: bool) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    max_closed_dd = 0.0
    used_margin = 0.0
    max_margin = 0.0
    allocated_risk = 0.0
    max_allocated_risk = 0.0
    max_allocated_risk_pct_balance = 0.0
    max_concurrent = 0
    skipped_margin = 0
    seq = 0
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
    by_module: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    daily: dict[str, float] = defaultdict(float)
    rows: list[dict[str, Any]] = []

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, max_closed_dd, used_margin, allocated_risk
        while open_heap and open_heap[0][0] <= moment:
            _, _, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - trade["margin"])
            allocated_risk = max(0.0, allocated_risk - trade["risk_usd"])
            balance += trade["pnl"]
            peak = max(peak, balance)
            max_closed_dd = min(max_closed_dd, balance - peak)
            module = trade["module"]
            by_module[module]["positions"] += 1
            by_module[module]["pnl"] += trade["pnl"]
            if trade["pnl"] > 0.005:
                by_module[module]["wins"] += 1
            elif trade["pnl"] < -0.005:
                by_module[module]["losses"] += 1
            else:
                by_module[module]["flat"] += 1
            daily[trade["closed"].date().isoformat()] += trade["pnl"]
            rows.append({
                "opened": trade["opened"].isoformat(),
                "closed": trade["closed"].isoformat(),
                "module": module,
                "lot": round(trade["lot"], 2),
                "risk_usd": round(trade["risk_usd"], 2),
                "pnl": round(trade["pnl"], 2),
                "balance": round(balance, 2),
            })

    for event in sorted(events, key=lambda row: (row["opened"], row["closed"])):
        close_until(event["opened"])
        risk_usd = max(0.0, balance) * risk_pct / 100.0
        raw_lot = risk_usd / max(0.01, float(event["loss_per_lot"]))
        lot = _normal(symbol, cfg, max(float(cfg.min_lot), raw_lot))
        actual_risk = lot * float(event["loss_per_lot"])
        margin = _margin(symbol, event["side"], lot, event["entry"])
        if balance <= 0.0 or (enforce_margin and margin > balance - used_margin + 0.01):
            skipped_margin += 1
            continue
        trade = {
            **event,
            "lot": lot,
            "risk_usd": actual_risk,
            "margin": margin,
            "pnl": float(event["pnl_per_lot"]) * lot,
        }
        seq += 1
        heapq.heappush(open_heap, (trade["closed"], seq, trade))
        used_margin += margin
        allocated_risk += actual_risk
        max_margin = max(max_margin, used_margin)
        max_allocated_risk = max(max_allocated_risk, allocated_risk)
        max_allocated_risk_pct_balance = max(
            max_allocated_risk_pct_balance,
            100.0 * allocated_risk / max(0.01, balance),
        )
        max_concurrent = max(max_concurrent, len(open_heap))

    close_until(datetime.max.replace(tzinfo=UTC))
    modules: dict[str, Any] = {}
    for module, values in sorted(by_module.items()):
        decided = values["wins"] + values["losses"]
        modules[module] = {
            "positions": int(values["positions"]),
            "wins": int(values["wins"]),
            "losses": int(values["losses"]),
            "flat": int(values["flat"]),
            "win_rate_pct": round(100.0 * values["wins"] / max(1.0, decided), 2),
            "pnl": round(values["pnl"], 2),
        }
    running_peak = float(start_balance)
    max_dd_pct_peak = 0.0
    minimum_balance = float(start_balance)
    for row in rows:
        current = float(row["balance"])
        running_peak = max(running_peak, current)
        minimum_balance = min(minimum_balance, current)
        max_dd_pct_peak = max(max_dd_pct_peak, 100.0 * (running_peak - current) / running_peak)
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "max_closed_drawdown_usd": round(abs(max_closed_dd), 2),
        "max_closed_drawdown_pct_of_start": round(100.0 * abs(max_closed_dd) / start_balance, 2),
        "max_closed_drawdown_pct_of_peak": round(max_dd_pct_peak, 2),
        "minimum_closed_balance": round(minimum_balance, 2),
        "max_concurrent_positions": max_concurrent,
        "max_allocated_initial_risk_usd": round(max_allocated_risk, 2),
        "max_allocated_initial_risk_pct_start": round(100.0 * max_allocated_risk / start_balance, 2),
        "max_allocated_initial_risk_pct_balance": round(max_allocated_risk_pct_balance, 2),
        "max_used_margin": round(max_margin, 2),
        "skipped_for_margin": skipped_margin,
        "by_module": modules,
        "daily_pnl": {key: round(value, 2) for key, value in sorted(daily.items())},
        "trades": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--phoenix", required=True)
    parser.add_argument("--bbmac", required=True)
    parser.add_argument("--start-balance", type=float, default=2000.0)
    parser.add_argument("--risk-pct-per-leg", type=float, default=1.5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    phoenix = _load(Path(args.phoenix))
    bbmac = _load(Path(args.bbmac))
    phoenix_events = _phoenix_events(phoenix)
    if not phoenix_events:
        raise RuntimeError("Phoenix report contains no trades")
    start = min(row["opened"] for row in phoenix_events)
    end = max(row["closed"] for row in phoenix_events)
    bbmac_events = _bbmac_events(bbmac, start, end)
    events = phoenix_events + bbmac_events

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        realistic = _simulate(events, symbol, cfg, args.start_balance, args.risk_pct_per_leg, True)
        theoretical = _simulate(events, symbol, cfg, args.start_balance, args.risk_pct_per_leg, False)
    finally:
        shutdown()

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "assumptions": {
            "start_balance": args.start_balance,
            "risk_pct_per_leg": args.risk_pct_per_leg,
            "phoenix": "all recorded current Phoenix full/range/direction legs",
            "bbmac": "current BBMAC breakout, one leg, 2.5 ATR SL, 3.2R TP, BE at 0.75R",
            "costs": "embedded historical spread and commission from source replays",
            "position_limit": "no artificial limit; realistic result enforces broker margin",
        },
        "event_counts": {"phoenix": len(phoenix_events), "bbmac": len(bbmac_events), "total": len(events)},
        "realistic": realistic,
        "theoretical_without_margin_limit": theoretical,
        "limitations": [
            "Drawdown is calculated from chronologically closed balances; exact intraminute floating-equity drawdown requires a full tick replay.",
            "BBMAC ATR stop distance is recovered from its original 1.5% risk sizing and may differ slightly because broker lot size is rounded to 0.01.",
            "Past results are not a forecast or guarantee of future profit.",
        ],
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"range_utc": output["range_utc"], "event_counts": output["event_counts"], "realistic": {key: value for key, value in realistic.items() if key not in {"trades", "daily_pnl"}}, "theoretical": {key: value for key, value in theoretical.items() if key not in {"trades", "daily_pnl"}}}, indent=2))


if __name__ == "__main__":
    main()
