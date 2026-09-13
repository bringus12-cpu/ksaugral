from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _phoenix_confirmed_range_legs,
    _phoenix_limit_pending_levels,
    _phoenix_range_pending_levels,
)
from scripts.backtest_phoenix_active_range import _completed_sessions, _ranges, _simulate_leg
from scripts.backtest_phoenix_complete_60d import _rates


def _summary(rows: list[dict]) -> dict:
    pnl = [float(row["pnl_001"]) for row in rows]
    wins = sum(value > 0 for value in pnl)
    losses = sum(value < 0 for value in pnl)
    gross_win = sum(max(0.0, value) for value in pnl)
    gross_loss = abs(sum(min(0.0, value) for value in pnl))
    return {
        "positions": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "pnl_001": round(sum(pnl), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        rates = _rates(symbol, end - timedelta(days=max(125, int(args.sessions * 2.0))), end + timedelta(hours=1))
        session_dates, cutoff = _completed_sessions(rates, end, int(args.sessions))
        rates = rates[(rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))].reset_index(drop=True)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        value_001 = abs(float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0))
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        ranges = await _ranges(cfg, cutoff, end, broker_offset)
        split = max(1, int(len(ranges) * 0.60))

        target_plans = ((1.0, 1.5, 2.0), (1.25, 2.0, 3.0), (1.25, 2.0, 5.0))
        stop_distances = (4.0, 6.0, 8.0, 10.0, 12.0)
        be_rules = ((0.30, 0.10, "fast"), (0.75, 0.15, "medium"), (1.25, 0.10, "late"), (999.0, 0.0, "none"))
        rows: list[dict] = []
        for targets in target_plans:
            for stop_distance in stop_distances:
                for be_trigger, be_buffer, be_name in be_rules:
                    trades: list[dict] = []
                    by_range: dict[int, list[dict]] = {}
                    skipped: Counter[str] = Counter()
                    for range_index, row in enumerate(ranges):
                        start_idx = int(rates["time"].searchsorted(pd.Timestamp(row["time"]).ceil("1min"), side="left"))
                        if start_idx >= len(rates):
                            skipped["no_market_bar"] += 1
                            continue
                        spread = float(rates.iloc[start_idx]["spread"]) * point
                        bid = float(rates.iloc[start_idx]["open"])
                        market = bid + spread if row["side"] == "buy" else bid
                        usable = market <= row["high"] + 0.50 if row["side"] == "buy" else market >= row["low"] - 0.50
                        pending = _phoenix_range_pending_levels(row["side"], [row["low"], row["high"]], market, 0.01)
                        pending = _phoenix_limit_pending_levels(row["side"], pending, 2)
                        plan = _phoenix_confirmed_range_legs(usable, market, pending, len(targets), False)
                        for leg_index, (kind, entry) in enumerate(plan):
                            trade = _simulate_leg(
                                rates, row, kind, float(entry), targets[min(leg_index, len(targets) - 1)],
                                stop_distance, point, value_001, float(args.commission_per_001), 15,
                                be_trigger, be_buffer,
                            )
                            if trade is None:
                                skipped["pending_not_filled"] += 1
                                continue
                            trade["range_index"] = range_index
                            trades.append(trade)
                            by_range.setdefault(range_index, []).append(trade)
                    train = [trade for trade in trades if int(trade["range_index"]) < split]
                    holdout = [trade for trade in trades if int(trade["range_index"]) >= split]
                    item = {
                        "targets_usd": targets,
                        "stop_usd": stop_distance,
                        "be": be_name,
                        "be_trigger_usd": be_trigger if be_name != "none" else None,
                        "be_buffer_usd": be_buffer,
                        "train": _summary(train),
                        "holdout": _summary(holdout),
                        "full": _summary(trades),
                        "skips": dict(skipped),
                    }
                    rows.append(item)

        robust = [row for row in rows if row["train"]["pnl_001"] > 0 and row["holdout"]["pnl_001"] > 0]
        robust.sort(key=lambda row: (row["holdout"]["pnl_001"], row["full"]["profit_factor"] or 0.0), reverse=True)
        current = next(
            row for row in rows
            if tuple(row["targets_usd"]) == (1.25, 2.0, 5.0) and row["stop_usd"] == 6.0 and row["be"] == "fast"
        )
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "sessions": session_dates,
            "ranges": len(ranges),
            "validation": "chronological 60% train / 40% holdout; spread and commission included; SL-first same M1 bar",
            "tested_configs": len(rows),
            "current": current,
            "best_robust": robust[0] if robust else None,
            "top10_robust": robust[:10],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(out), "ranges": len(ranges), "current": current, "best": output["best_robust"]}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
