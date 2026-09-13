from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, calc_loss_per_lot, connect, ensure_symbol, shutdown
from scripts.simulate_phoenix_bbmac_risk_per_leg import _dt, _simulate


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _phoenix_events(path: str, module: str, source_filter: str = "") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in _load(path).get("trades", []):
        if source_filter and str(row.get("source", "")) != source_filter:
            continue
        entry = float(row.get("entry", 0.0) or 0.0)
        sl = float(row.get("sl", 0.0) or 0.0)
        if entry <= 0.0 or sl <= 0.0 or abs(entry - sl) < 0.01:
            continue
        events.append(
            {
                "module": module,
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row.get("side", "buy")),
                "entry": entry,
                "sl": sl,
                "pnl_001": float(row.get("pnl_001", row.get("pnl", 0.0)) or 0.0),
                "status": str(row.get("status", "")),
                "message_id": int(row.get("message_id", 0) or 0),
                "target_index": int(row.get("target_index", 0) or 0),
                "fill_kind": str(row.get("fill_kind", "")),
            }
        )
    return events


def _scalper_events(path: str, module: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in _load(path).get("trades", []):
        old_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        old_pnl = float(row.get("profit", 0.0) or 0.0)
        reference_balance = max(0.01, float(row.get("balance", 0.0) or 0.0) - old_pnl)
        events.append(
            {
                "module": module,
                "opened": _dt(row["opened"]),
                "closed": _dt(row["closed"]),
                "side": str(row.get("side", "buy")),
                "entry": float(row.get("entry", 0.0) or 0.0),
                "loss_per_lot": reference_balance * 0.015 / old_lot,
                "pnl_per_lot": old_pnl / old_lot,
                "status": str(row.get("status", "")),
            }
        )
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--start-balance", type=float, default=2000.0)
    parser.add_argument("--risk-pct-per-leg", type=float, default=1.5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    inputs = {
        "phoenix_range": "data_vantage/phoenix_active_range_60sessions_20260903.json",
        "phoenix_direction": "data_vantage/phoenix_direction_current_60sessions_20260903.json",
        "phoenix_tp1_runner": "data_vantage/phoenix_tp1_runner_cap12_reward15_60sessions_20260903.json",
        "phoenix_profit": "data_vantage/phoenix_profit_variant_60sessions_20260903.json",
        "phoenix_extra_market": "data_vantage/phoenix_extra_market_tp2_tp5_60sessions_20260903.json",
        "scalper_db60": "data_vantage/scalper_db60_optimized_risk15_60sessions_20260903.json",
        "scalper_bbkelt": "data_vantage/scalper_bbkelt_profit_risk15_corrected_60sessions_20260903.json",
    }
    full_report = _load("data_vantage/phoenix_current_full6_60sessions_20260903.json")
    start = _dt(full_report["range"]["start"])
    end = _dt(full_report["range"]["end"])

    raw_events = [
        *_phoenix_events(inputs["phoenix_range"], "phoenix_range"),
        *_phoenix_events(inputs["phoenix_direction"], "phoenix_direction"),
        *_phoenix_events(inputs["phoenix_tp1_runner"], "phoenix_tp1_runner", "tp1_runner"),
        *_phoenix_events(inputs["phoenix_profit"], "phoenix_profit"),
        *_phoenix_events(inputs["phoenix_extra_market"], "phoenix_extra_market"),
        *_scalper_events(inputs["scalper_db60"], "scalper_db60"),
        *_scalper_events(inputs["scalper_bbkelt"], "scalper_bbkelt"),
    ]
    raw_events = [row for row in raw_events if start <= row["opened"] <= end]

    env_paths = args.env or [".env.vantage"]
    for env_path in env_paths:
        load_dotenv(env_path, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        events: list[dict[str, Any]] = []
        for row in raw_events:
            if "loss_per_lot" in row:
                events.append(row)
                continue
            loss_per_lot = calc_loss_per_lot(symbol, row["side"], row["entry"], row["sl"])
            if loss_per_lot <= 0.0:
                continue
            events.append(
                {
                    "module": row["module"],
                    "opened": row["opened"],
                    "closed": row["closed"],
                    "side": row["side"],
                    "entry": row["entry"],
                    "loss_per_lot": loss_per_lot,
                    "pnl_per_lot": row["pnl_001"] * 100.0,
                    "status": row["status"],
                }
            )
        realistic = _simulate(events, symbol, cfg, args.start_balance, args.risk_pct_per_leg, True)
        theoretical = _simulate(events, symbol, cfg, args.start_balance, args.risk_pct_per_leg, False)
    finally:
        shutdown()

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "module_count": len(inputs),
        "modules": list(inputs),
        "input_event_count": len(events),
        "assumptions": {
            "start_balance": args.start_balance,
            "risk_pct_per_leg": args.risk_pct_per_leg,
            "lot_rule": "each entry risks 1.5% of current closed balance, normalized to broker lot step",
            "position_limit": "none",
            "costs": "dynamic historical spread and source-report commission",
        },
        "realistic_with_broker_margin": realistic,
        "without_margin_filter": theoretical,
        "inputs": inputs,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({
        "range_utc": output["range_utc"],
        "module_count": output["module_count"],
        "input_event_count": output["input_event_count"],
        "realistic": {key: value for key, value in realistic.items() if key not in {"trades", "daily_pnl"}},
        "without_margin_filter": {key: value for key, value in theoretical.items() if key not in {"trades", "daily_pnl"}},
    }, indent=2))


if __name__ == "__main__":
    main()
