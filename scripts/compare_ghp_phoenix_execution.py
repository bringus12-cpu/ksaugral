from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.ghp_parser import parse_ghp_message
from app.mt5_gateway import Mt5Credentials, calc_loss_per_lot, connect, ensure_symbol, shutdown
from scripts.audit_ghp_parsers_90sessions import _attach_actions, _deduplicate, _resolve_symbol, _simulate
from scripts.backtest_phoenix_complete_60d import _rates
from scripts.simulate_current_stack_with_ghp_60sessions import _dt, _portfolio


def _load_rows(path: Path, channel: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            if str(raw.get("channel", "")) != channel:
                continue
            moment = _dt(raw["date"])
            if not start <= moment <= end:
                continue
            parsed = parse_ghp_message(raw.get("text", ""), raw.get("title", ""))
            signal = vars(parsed.signal).copy() if parsed.signal else None
            rows.append({**raw, "date_dt": moment, "parsed": {**vars(parsed), "signal": signal}})
    _attach_actions(rows)
    unique, _ = _deduplicate([row for row in rows if row["parsed"].get("signal")])
    return unique


def _events(
    rows: list[dict[str, Any]],
    frames: dict[str, Any],
    symbols: dict[str, str],
    targets: tuple[int, ...],
    market_fill: bool,
    be_after_tp1: bool,
    assets: set[str] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        signal = row["parsed"]["signal"]
        asset = str(signal["asset"])
        if assets and asset not in assets:
            continue
        frame = frames.get(asset)
        symbol = symbols.get(asset)
        if frame is None or frame.empty or not symbol:
            continue
        simulation_row = row
        if market_fill:
            idx = int(frame["time"].searchsorted(row["date_dt"], side="left"))
            if idx >= len(frame):
                continue
            fill = float(frame.iloc[idx]["open"])
            tps = [float(value) for value in signal.get("tps", [])]
            sl = float(signal.get("sl", 0.0) or 0.0)
            side = str(signal.get("side", ""))
            target_ahead = tps and (fill < tps[0] if side == "buy" else fill > tps[0])
            valid_sl = sl < fill if side == "buy" else sl > fill
            if not target_ahead or not valid_sl:
                continue
            shifted = {**signal, "entry": fill, "entries": [fill], "order_kind": "market"}
            simulation_row = {**row, "parsed": {**row["parsed"], "signal": shifted}}
        # Early provider BE messages are deliberately ignored. The runner can
        # move only after the M1 series confirms a real TP1 touch.
        simulation_row = {
            **simulation_row,
            "actions": [action for action in row.get("actions", []) if action.get("kind") != "breakeven"],
        }
        for target in targets:
            outcome = _simulate(simulation_row, frame, symbol, target, 60, be_after_tp1 and target > 1)
            if outcome.get("status") in {"expired", "cancelled", "invalid", "no_rates"} or "opened" not in outcome:
                continue
            used_signal = simulation_row["parsed"]["signal"]
            loss = calc_loss_per_lot(symbol, used_signal["side"], outcome["entry"], outcome["sl"])
            if loss <= 0:
                continue
            events.append(
                {
                    "module": "ghp_currency",
                    "symbol": symbol,
                    "asset": asset,
                    "side": used_signal["side"],
                    "entry": outcome["entry"],
                    "opened": outcome["opened"],
                    "closed": outcome["closed"],
                    "loss_per_lot": loss,
                    "pnl_per_lot": float(outcome["pnl_001"]) * 100.0,
                    "status": outcome["status"],
                }
            )
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.puprime.live")
    parser.add_argument("--messages", required=True)
    parser.add_argument("--channel", default="-1003495213392")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--start-balance", type=float, default=1100.0)
    parser.add_argument("--risk-pct", type=float, default=1.5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        probe_symbol = ensure_symbol(cfg.symbol)
        from app.mt5_gateway import mt5

        probe = mt5.copy_rates_from_pos(probe_symbol, mt5.TIMEFRAME_D1, 0, args.sessions + 30)
        dates = sorted({datetime.fromtimestamp(int(item["time"]), UTC).date() for item in probe})[-args.sessions:]
        start = datetime.combine(dates[0], datetime.min.time(), tzinfo=UTC)
        end = datetime.now(UTC)
        split = start + (end - start) / 2
        rows = _load_rows(Path(args.messages), args.channel, start, end)
        assets = sorted({row["parsed"]["signal"]["asset"] for row in rows})
        symbols = {asset: _resolve_symbol(asset) for asset in assets}
        frames = {
            asset: _rates(symbol, start - timedelta(hours=4), end + timedelta(hours=73))
            for asset, symbol in symbols.items()
            if symbol
        }
        variants = {
            "quoted_tp1_tp2": ((1, 2), False, True, None),
            "market_tp1_only": ((1,), True, False, None),
            "market_tp1_tp2": ((1, 2), True, True, None),
            "market_phoenix_tp1_tp1_tp2": ((1, 1, 2), True, True, None),
            "market_tp1_tp2_no_be": ((1, 2), True, False, None),
            "market_stable_assets_tp1_tp2": ((1, 2), True, True, {"usdchf", "euraud", "eurusd"}),
        }
        report: dict[str, Any] = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": start.isoformat(), "end": end.isoformat(), "sessions": args.sessions},
            "channel": args.channel,
            "signals": len(rows),
            "variants": {},
        }
        for name, (targets, market_fill, be, allowed_assets) in variants.items():
            events = _events(rows, frames, symbols, targets, market_fill, be, allowed_assets)
            report["variants"][name] = {
                "all": _portfolio(events, cfg, args.start_balance, args.risk_pct, True),
                "first_half": _portfolio([event for event in events if event["opened"] < split], cfg, args.start_balance, args.risk_pct, True),
                "second_half": _portfolio([event for event in events if event["opened"] >= split], cfg, args.start_balance, args.risk_pct, True),
            }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
