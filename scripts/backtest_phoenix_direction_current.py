from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _phoenix_direction_pullback_confirmed,
    _phoenix_preliminary_reconcile_reason,
)
from scripts.backtest_phoenix_active_profile import _completed_sessions
from scripts.backtest_phoenix_complete_60d import _fetch_history, _rates


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _summary(trades: list[dict[str, Any]], announcements: int, confirmed: int) -> dict[str, Any]:
    pnl = [float(row["pnl_001"]) for row in trades]
    wins = sum(value > 0.005 for value in pnl)
    losses = sum(value < -0.005 for value in pnl)
    gross_win = sum(max(0.0, value) for value in pnl)
    gross_loss = abs(sum(min(0.0, value) for value in pnl))
    balance = peak = 0.0
    max_dd = 0.0
    for row in sorted(trades, key=lambda item: item["exit_time"]):
        balance += float(row["pnl_001"])
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
    return {
        "announcements": announcements,
        "pullback_confirmed": confirmed,
        "positions": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "pnl_001": round(sum(pnl), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_001": round(max_dd, 2),
        "status": dict(Counter(str(row["status"]) for row in trades)),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/audit_current_phoenix_direction_60sessions.json")
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(args.broker_offset_hours)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        rates = _rates(
            symbol,
            end - timedelta(days=max(125, int(args.sessions * 2.0))),
            end + timedelta(hours=1),
        )
        session_dates, cutoff = _completed_sessions(rates, end, int(args.sessions))
        rates = rates[(rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))].reset_index(drop=True)
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, announcements = await _fetch_history(rates, cutoff, session)
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        tp_distance = max(0.1, float(os.getenv("PHOENIX_DIRECTION_RUNNER_TP_USD", "1.0") or 1.0))
        sl_distance = max(0.1, float(os.getenv("PHOENIX_DIRECTION_RUNNER_SL_USD", "6.0") or 6.0))
        max_hold = max(1.0, float(os.getenv("PHOENIX_DIRECTION_RUNNER_MAX_HOLD_MINUTES", "15") or 15.0))
        be_trigger = max(0.1, float(os.getenv("PHOENIX_RANGE_TRIGGER_BE_TRIGGER_USD", "0.30") or 0.30))
        be_buffer = max(0.0, float(os.getenv("PHOENIX_RANGE_TRIGGER_BE_BUFFER_USD", "0.10") or 0.10))
        reconcile_gap = max(0.0, float(os.getenv("PHOENIX_PRELIMINARY_RECONCILE_GAP_USD", "2.0") or 2.0))
        times = rates["time"].to_numpy(dtype="datetime64[ns]")
        signal_rows = sorted(signals, key=lambda row: row.start_idx)
        trades: list[dict[str, Any]] = []
        confirmed_count = 0

        for announcement in sorted(announcements, key=lambda row: row["time"]):
            side = str(announcement.get("side") or "")
            broker_time = announcement["time"] + broker_offset
            start_idx = int(rates["time"].searchsorted(pd.Timestamp(broker_time).ceil("1min"), side="left"))
            if side not in {"buy", "sell"} or start_idx >= len(rates):
                continue
            closed = rates.iloc[max(0, start_idx - 40) : start_idx]["close"].astype(float).tolist()
            if not _phoenix_direction_pullback_confirmed(side, closed):
                continue
            confirmed_count += 1

            spread = float(rates.iloc[start_idx]["spread"]) * point
            bid = float(rates.iloc[start_idx]["open"])
            entry = bid + spread if side == "buy" else bid
            target = entry + tp_distance if side == "buy" else entry - tp_distance
            initial_sl = entry - sl_distance if side == "buy" else entry + sl_distance
            current_sl = initial_sl
            expiry = times[start_idx] + np.timedelta64(int(round(max_hold * 60.0)), "s")
            end_idx = min(int(np.searchsorted(times, expiry, side="right")), len(rates))
            exit_idx = max(start_idx, end_idx - 1)
            last_spread = float(rates.iloc[exit_idx]["spread"]) * point
            exit_price = float(rates.iloc[exit_idx]["close"]) + (last_spread if side == "sell" else 0.0)
            status = "timeout"

            relevant_signals = [
                row
                for row in signal_rows
                if start_idx <= int(row.start_idx) < end_idx
                and timedelta(0) <= row.time - announcement["time"] <= timedelta(minutes=20)
            ]
            signal_by_idx = {int(row.start_idx): row for row in relevant_signals}
            for index in range(start_idx, end_idx):
                row = rates.iloc[index]
                bar_spread = float(row["spread"]) * point
                exit_high = float(row["high"]) + (bar_spread if side == "sell" else 0.0)
                exit_low = float(row["low"]) + (bar_spread if side == "sell" else 0.0)
                hit_sl = exit_low <= current_sl if side == "buy" else exit_high >= current_sl
                hit_tp = exit_high >= target if side == "buy" else exit_low <= target
                if hit_sl:
                    status, exit_idx, exit_price = "stop", index, current_sl
                    break
                if hit_tp:
                    status, exit_idx, exit_price = "target", index, target
                    break

                full = signal_by_idx.get(index)
                if full is not None:
                    current_price = float(row["open"]) + (bar_spread if side == "sell" else 0.0)
                    floating = _profit(symbol, side, 0.01, entry, current_price)
                    reason = _phoenix_preliminary_reconcile_reason(
                        side,
                        entry,
                        floating,
                        str(full.signal.side),
                        list(full.signal.entries),
                        reconcile_gap,
                    )
                    if reason:
                        status, exit_idx, exit_price = f"reconcile:{reason}", index, current_price
                        break

                favorable = exit_high - entry if side == "buy" else entry - exit_low
                if favorable >= be_trigger:
                    candidate = entry + be_buffer if side == "buy" else entry - be_buffer
                    current_sl = max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)

            pnl = _profit(symbol, side, 0.01, entry, exit_price) - float(args.commission_per_001)
            trades.append(
                {
                    "message_id": int(announcement["message_id"]),
                    "signal_time": announcement["time"].isoformat(),
                    "entry_time": rates.iloc[start_idx]["time"].isoformat(),
                    "exit_time": rates.iloc[exit_idx]["time"].isoformat(),
                    "side": side,
                    "entry": round(entry, 3),
                    "sl": round(initial_sl, 3),
                    "tp": round(target, 3),
                    "status": status,
                    "pnl_001": round(pnl, 4),
                }
            )

        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions_included": session_dates,
            "symbol": symbol,
            "method": (
                "Current Phoenix direction pre-signal replay: next complete broker M1 bar, EMA9/21 pullback confirmation, "
                "dynamic historical spread, TP/SL from active profile, BE protection shared with range positions, "
                "15-minute hard timeout, preliminary/full-signal reconciliation and commission; conservative SL-first ordering"
            ),
            "config": {
                "tp_usd": tp_distance,
                "sl_usd": sl_distance,
                "max_hold_minutes": max_hold,
                "be_trigger_usd": be_trigger,
                "be_buffer_usd": be_buffer,
                "reconcile_gap_usd": reconcile_gap,
                "commission_per_001": float(args.commission_per_001),
            },
            "summary": _summary(trades, len(announcements), confirmed_count),
            "trades": sorted(trades, key=lambda row: row["entry_time"]),
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(out), "summary": output["summary"]}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
