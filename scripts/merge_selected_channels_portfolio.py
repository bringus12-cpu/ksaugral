from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, calc_loss_per_lot, connect, ensure_symbol, shutdown
from scripts.simulate_current_stack_with_ghp_60sessions import _portfolio


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _target_group(value: int) -> str:
    if int(value) == 1:
        return "tp1"
    if int(value) == 2:
        return "tp2"
    return "deep"


def _is_mirror(candidate: dict[str, Any], accepted: list[dict[str, Any]]) -> bool:
    opened = candidate["opened"]
    for other in reversed(accepted):
        seconds = (opened - other["opened"]).total_seconds()
        if seconds > 20 * 60:
            break
        if seconds < 0:
            continue
        if candidate["module"] == other["module"]:
            continue
        if _symbol_key(candidate["symbol"]) != _symbol_key(other["symbol"]) or candidate["side"] != other["side"]:
            continue
        if _target_group(candidate["target_index"]) != _target_group(other["target_index"]):
            continue
        if abs(candidate["entry"] - other["entry"]) > 1.5:
            continue
        if candidate["sl"] > 0 and other["sl"] > 0 and abs(candidate["sl"] - other["sl"]) > 2.0:
            continue
        return True
    return False


def _symbol_key(symbol: str) -> str:
    # Compare known broker aliases without merging distinct 24/7 contracts.
    raw = symbol.upper()
    return {"XAUUSD+": "XAUUSD", "XAUUSD.S": "XAUUSD", "DJ30": "US30"}.get(raw, raw)


def _base_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in payload["realistic_with_broker_min_lot_and_margin"]["trade_sequence"]:
        lot = max(0.0001, float(row.get("lot", 0.0) or 0.0))
        loss_per_lot = float(row.get("initial_risk", 0.0) or 0.0) / lot
        if loss_per_lot <= 0:
            continue
        output.append(
            {
                "module": str(row["module"]),
                "opened": _dt(row["opened"]),
                "closed": _dt(row["closed"]),
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["sl"]),
                "loss_per_lot": loss_per_lot,
                "pnl_per_lot": float(row["pnl_per_lot"]),
                "status": str(row.get("status", "")),
                "message_id": int(row.get("message_id", 0) or 0),
                "target_index": int(row.get("target_index", 0) or 0),
                "fill_kind": str(row.get("fill_kind", "")),
                "risk_pct_override": 1.75,
            }
        )
    return output


def _candidate_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for channel in payload.get("channels", []):
        username = str(channel.get("username") or channel.get("id") or "unknown")
        for row in channel.get("trade_events", []):
            sl = float(row.get("initial_sl", 0.0) or 0.0)
            entry = float(row.get("entry", 0.0) or 0.0)
            if entry <= 0 or sl <= 0 or abs(entry - sl) < 0.01:
                continue
            output.append(
                {
                    "module": f"newtg:{username}",
                    "opened": _dt(row["opened"]),
                    "closed": _dt(row["closed"]),
                    "symbol": str(row["symbol"]),
                    "side": str(row["side"]),
                    "entry": entry,
                    "sl": sl,
                    "pnl_per_lot": float(row["p001"]) * 100.0,
                    "status": str(row.get("status", "")),
                    "message_id": int(row.get("message_id", 0) or 0),
                    "target_index": int(row.get("target", 0) or 0),
                    "fill_kind": "provider_level",
                    "risk_pct_override": 1.75,
                }
            )
    return sorted(output, key=lambda row: (row["opened"], row["closed"], row["module"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--base-report", required=True)
    parser.add_argument("--channels-report", required=True)
    parser.add_argument("--start-balance", type=float, default=700.0)
    parser.add_argument("--risk-pct-per-leg", type=float, default=1.75)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(ROOT / args.env, override=True)
    cfg = load_settings()
    base_payload = json.loads((ROOT / args.base_report).read_text(encoding="utf-8"))
    channel_payload = json.loads((ROOT / args.channels_report).read_text(encoding="utf-8"))
    base = _base_events(base_payload)
    candidates = _candidate_events(channel_payload)

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        for event in candidates:
            event["symbol"] = ensure_symbol(event["symbol"])
            event["loss_per_lot"] = calc_loss_per_lot(event["symbol"], event["side"], event["entry"], event["sl"])
        existing = sorted(
            [row for row in base if row["module"].startswith("phoenix_") or row["module"].startswith("ghp:")],
            key=lambda row: row["opened"],
        )
        accepted: list[dict[str, Any]] = []
        mirror_counts: Counter[str] = Counter()
        for event in candidates:
            comparison = sorted(existing + accepted, key=lambda row: row["opened"])
            if _is_mirror(event, comparison):
                mirror_counts[event["module"]] += 1
                continue
            accepted.append(event)
        events = base + accepted
        events = [{**row, "risk_pct_override": args.risk_pct_per_leg} for row in events]
        result = _portfolio(events, cfg, args.start_balance, args.risk_pct_per_leg, True)
    finally:
        shutdown()

    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "base_report": args.base_report,
        "channels_report": args.channels_report,
        "assumptions": {
            "start_balance": args.start_balance,
            "risk_pct_per_leg": args.risk_pct_per_leg,
            "sizing_basis": "closed_balance_not_equity",
            "forecast_eligible": False,
            "cross_channel_mirror_window_minutes": 20,
            "entry_tolerance": 1.5,
            "sl_tolerance": 2.0,
        },
        "base_events": len(base),
        "new_candidate_events": len(candidates),
        "new_events_after_deduplication": len(accepted),
        "duplicates_removed": len(candidates) - len(accepted),
        "duplicates_removed_by_module": dict(mirror_counts),
        "result": result,
        "realistic_with_broker_min_lot_and_margin": result,
        "limitations": [
            "Cross-channel mirroring is inferred from time and price similarity; attribution can be imperfect.",
            "The replay uses final Telegram message text and cannot reconstruct every historical edit.",
            "Closed-balance drawdown is reported here; floating M1 stress is calculated separately.",
        ],
    }
    output = ROOT / args.output
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
