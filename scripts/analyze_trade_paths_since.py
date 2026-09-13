from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import MetaTrader5 as mt5
import pandas as pd


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pnl_at(side: str, symbol: str, volume: float, entry: float, price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, volume, entry, price) or 0.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-env", default="")
    parser.add_argument("--env", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--start", default="2026-09-04T00:00:00+00:00")
    parser.add_argument("--broker-time-offset-minutes", type=int, default=180)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=True)
    if args.base_env:
        load_dotenv(root / args.base_env, override=True)
    load_dotenv(root / args.env, override=True)

    import os

    if not mt5.initialize(path=str(os.getenv("MT5_PATH", "") or "")):
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
    try:
        report = json.loads((root / args.report).read_text(encoding="utf-8-sig"))
        start = parse_time(args.start)
        offset = timedelta(minutes=args.broker_time_offset_minutes)
        rows = [row for row in report.get("positions", []) if parse_time(row["closed_utc"]) >= start]
        now = datetime.now(UTC)
        rates_by_symbol: dict[str, pd.DataFrame] = {}
        for symbol in sorted({str(row["symbol"]) for row in rows}):
            symbol_rows = [row for row in rows if row["symbol"] == symbol and row.get("opened_utc")]
            if not symbol_rows:
                continue
            first = min(parse_time(row["opened_utc"]) for row in symbol_rows) + offset - timedelta(minutes=2)
            raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, first, now + offset + timedelta(minutes=2))
            frame = pd.DataFrame([] if raw is None else raw)
            if frame.empty:
                continue
            frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True) - pd.Timedelta(offset)
            rates_by_symbol[symbol] = frame.sort_values("time").reset_index(drop=True)

        output_rows = []
        for row in rows:
            frame = rates_by_symbol.get(str(row["symbol"]))
            if frame is None or frame.empty or not row.get("opened_utc"):
                continue
            opened = pd.Timestamp(parse_time(row["opened_utc"]))
            closed = pd.Timestamp(parse_time(row["closed_utc"]))
            entry = float(row.get("entry_price", 0.0) or 0.0)
            volume = float(row.get("volume", 0.0) or 0.0)
            side = str(row.get("side") or "")
            during = frame[(frame["time"] >= opened) & (frame["time"] <= closed)]
            after = frame[(frame["time"] > closed) & (frame["time"] <= closed + pd.Timedelta(hours=4))]
            if during.empty or entry <= 0 or volume <= 0 or side not in {"buy", "sell"}:
                continue
            favorable_during = float(during["high"].max()) if side == "buy" else float(during["low"].min())
            adverse_during = float(during["low"].min()) if side == "buy" else float(during["high"].max())
            result = dict(row)
            result["mfe_during_usd"] = round(pnl_at(side, row["symbol"], volume, entry, favorable_during), 2)
            result["mae_during_usd"] = round(pnl_at(side, row["symbol"], volume, entry, adverse_during), 2)
            for minutes in (15, 60, 240):
                window = after[after["time"] <= closed + pd.Timedelta(minutes=minutes)]
                if window.empty:
                    result[f"best_if_held_{minutes}m_usd"] = None
                    result[f"close_if_held_{minutes}m_usd"] = None
                    continue
                best_price = float(window["high"].max()) if side == "buy" else float(window["low"].min())
                last_price = float(window.iloc[-1]["close"])
                result[f"best_if_held_{minutes}m_usd"] = round(pnl_at(side, row["symbol"], volume, entry, best_price), 2)
                result[f"close_if_held_{minutes}m_usd"] = round(pnl_at(side, row["symbol"], volume, entry, last_price), 2)
            output_rows.append(result)

        summary = {
            "positions_analyzed": len(output_rows),
            "manual_closes": sum("manual" in str(row.get("reason_name")) for row in output_rows),
            "manual_close_realized_pnl": round(sum(float(row["profit"]) for row in output_rows if "manual" in str(row.get("reason_name"))), 2),
        }
        payload = {"start_utc": args.start, "summary": summary, "positions": output_rows}
        target = root / args.output
        target.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        print(target)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
