from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_phoenix_complete_60d import _fetch_history, _rates
from scripts.optimize_phoenix_copy_execution import (
    Candidate,
    PROTECT_PROFILES,
    _channel_cancel_times,
    _run_candidate,
    _summary,
)


def _completed_sessions(rates: pd.DataFrame, end: datetime, requested: int) -> tuple[list[str], datetime]:
    end_ts = pd.Timestamp(end)
    session_key = (rates["time"] + pd.Timedelta(hours=2)).dt.date
    counts = rates.loc[rates["time"] < end_ts].groupby(session_key).size()
    completed = [day for day, count in counts.items() if int(count) >= 180]
    if len(completed) < requested:
        raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {requested}")
    selected = completed[-requested:]
    selected_mask = session_key.isin(selected)
    start = rates.loc[selected_mask, "time"].iloc[0].to_pydatetime()
    return [day.isoformat() for day in selected], start


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--signal-env", default=".env.vantage.signal")
    parser.add_argument("--telegram-session")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--entry-map", choices=["legacy", "side_deep", "cycle", "near", "middle"])
    parser.add_argument("--pending-minutes", type=int)
    parser.add_argument("--provider-sl-cap", type=float)
    parser.add_argument("--protect-profile", choices=sorted(PROTECT_PROFILES), default="fast_be")
    parser.add_argument("--no-channel-cancel", action="store_true")
    parser.add_argument(
        "--output",
        default="data_vantage/current_phoenix_active_60sessions_20260810.json",
    )
    args = parser.parse_args()

    for env_file in (args.env, args.signal_env):
        load_dotenv(Path(env_file).resolve(), override=True)
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(args.broker_offset_hours)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        probe_start = end - timedelta(days=max(125, int(args.sessions * 2.0)))
        rates = _rates(symbol, probe_start, end + timedelta(hours=1))
        session_dates, cutoff = _completed_sessions(rates, end, int(args.sessions))
        rates = rates[(rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))].reset_index(drop=True)

        session = (
            Path(args.telegram_session)
            if args.telegram_session
            else cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        )
        signals, _announcements = await _fetch_history(rates, cutoff, session)
        signals = sorted(
            [item for item in signals if len(item.signal.entries) >= 2 and len(item.signal.tps) >= 1],
            key=lambda item: item.time,
        )
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        cancel_times = await _channel_cancel_times(cfg, cutoff, broker_offset)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate XAUUSD value for 0.01 lot")

        candidate = Candidate(
            entry_map=str(args.entry_map or os.getenv("PHOENIX_ENTRY_TARGET_MAPPING", "near") or "near"),
            pending_minutes=int(
                args.pending_minutes
                if args.pending_minutes is not None
                else float(os.getenv("PHOENIX_PENDING_EXPIRY_MINUTES", "120") or 120)
            ),
            provider_sl_cap=float(
                args.provider_sl_cap
                if args.provider_sl_cap is not None
                else os.getenv("PHOENIX_PROVIDER_SL_MAX_DISTANCE_USD", "0") or 0.0
            ),
            protect_profile=str(args.protect_profile),
            channel_cancel=not bool(args.no_channel_cancel),
        )
        trades = _run_candidate(
            candidate,
            signals,
            rates,
            symbol,
            point,
            cancel_times,
            float(args.commission_per_001),
            profit_per_usd_001,
        )
        signal_ids = [int(item.message_id) for item in signals]
        output: dict[str, Any] = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions_included": session_dates,
            "symbol": symbol,
            "method": (
                "Active Phoenix full-signal profile on broker M1: configured entry map, TP1-TP6, "
                "configured provider SL cap, BE/ladder profile and pending expiry, optional channel cancellation, "
                "spread from each M1 bar and commission included; conservative SL-first same-bar ordering"
            ),
            "candidate": candidate.__dict__,
            "profit_per_usd_001": profit_per_usd_001,
            "summary": _summary(trades, signal_ids),
            "parsed_signal_ids": signal_ids,
            "trades": sorted(trades, key=lambda row: (row["entry_time"], row["target_index"])),
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(out), "range": output["range_utc"], "summary": output["summary"]}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
