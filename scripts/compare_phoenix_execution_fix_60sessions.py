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

import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import (
    _phoenix_entry_brain,
    _phoenix_market_runner_allowed,
    _phoenix_pending_stage_allowed,
    _phoenix_wait_for_zone_retrace,
)
from scripts.backtest_phoenix_active_profile import _completed_sessions
from scripts.backtest_phoenix_complete_60d import _fetch_history, _rates
from scripts.optimize_phoenix_copy_execution import (
    _entry_plan,
    _simulate_leg,
    _summary,
)


TARGET_PLAN = ((1, "none"), (5, "ladder"), (6, "ladder"))


def _old_wait_for_zone_retrace(brain: dict) -> bool:
    """Behavior deployed before the 2026-08-14 fresh-zone repair."""
    always_stage = str(os.getenv("PHOENIX_ALWAYS_STAGE_RANGE_PENDING", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return bool(
        str(brain.get("zone_state") or "") == "after_zone"
        and (always_stage or int(brain.get("reached_level", 0) or 0) == 0)
        and float(brain.get("zone_distance", 0.0) or 0.0) > 0.0
    )


def _market_at_signal(rates: pd.DataFrame, start_idx: int, side: str, point: float) -> float:
    bar = rates.iloc[start_idx]
    bid = float(bar["open"])
    spread = float(bar["spread"]) * point
    return bid + spread if side == "buy" else bid


def _execution_plan(item, rates: pd.DataFrame, point: float, variant: str) -> tuple[list[dict[str, Any]], str]:
    signal = item.signal
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
    if len(entries) < 2 or not signal.tps or item.start_idx >= len(rates):
        return [], "invalid_signal"
    market = _market_at_signal(rates, int(item.start_idx), signal.side, point)
    brain = _phoenix_entry_brain(signal, market, entries)
    retrace = (
        _old_wait_for_zone_retrace(brain)
        if variant == "previous_near_no_extra_market"
        else _phoenix_wait_for_zone_retrace(brain)
    )
    stage_allowed = _phoenix_pending_stage_allowed(
        signal,
        brain,
        retrace,
        len(entries),
        preliminary_range_matched=False,
    )
    if brain.get("decision") in {"skip_too_late_after_tp", "skip_not_enough_live_tps"} and not stage_allowed:
        return [], str(brain.get("decision"))

    market_allowed = bool(
        not retrace
        and _phoenix_market_runner_allowed(signal.side, entries, market, signal.tps)
    )
    entry_mode = "near" if variant == "previous_near_no_extra_market" else "cycle"
    planned_entries = _entry_plan(signal.side, entries, len(TARGET_PLAN), entry_mode)
    pending = [
        {
            "kind": "pending",
            "entry": float(entry),
            "target_index": int(target_index),
            "protect_mode": protect_mode,
        }
        for entry, (target_index, protect_mode) in zip(planned_entries, TARGET_PLAN)
    ]
    if not market_allowed:
        return pending, "pending_only"

    market_leg = {
        "kind": "market",
        "entry": market,
        "target_index": 1,
        "protect_mode": "none",
    }
    if variant == "previous_near_no_extra_market":
        # The old profile replaced the nearest TP1 pending with the market leg.
        return [market_leg, *pending[1:]], "market_replaces_tp1_pending"
    # The repaired profile keeps all three range levels and adds market TP1.
    return [market_leg, *pending], "market_plus_three_range_entries"


def _replay_variant(
    *,
    variant: str,
    signals: list,
    rates: pd.DataFrame,
    symbol: str,
    point: float,
    commission_per_001: float,
    profit_per_usd_001: float,
    pending_minutes: int,
    provider_sl_cap: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    times = rates["time"].to_numpy(dtype="datetime64[ns]")
    opens = rates["open"].to_numpy(dtype=float)
    highs = rates["high"].to_numpy(dtype=float)
    lows = rates["low"].to_numpy(dtype=float)
    closes = rates["close"].to_numpy(dtype=float)
    spreads = rates["spread"].to_numpy(dtype=float)
    trades: list[dict[str, Any]] = []
    decisions: Counter[str] = Counter()
    for item in signals:
        plan, decision = _execution_plan(item, rates, point, variant)
        decisions[decision] += 1
        for leg in plan:
            trade = _simulate_leg(
                rates=rates,
                times=times,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                spreads=spreads,
                point=point,
                symbol=symbol,
                item=item,
                entry=float(leg["entry"]),
                target_index=int(leg["target_index"]),
                protect_mode=str(leg["protect_mode"]),
                pending_minutes=int(pending_minutes),
                provider_sl_cap=float(provider_sl_cap),
                cancel_time=None,
                commission_per_001=float(commission_per_001),
                profit_per_usd_001=float(profit_per_usd_001),
                market_runner_override=str(leg["kind"]) == "market",
            )
            if trade is not None:
                trade["planned_kind"] = str(leg["kind"])
                trade["variant"] = variant
                trades.append(trade)
    return trades, dict(decisions)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--pending-minutes", type=int, default=15)
    parser.add_argument("--provider-sl-cap", type=float, default=12.0)
    parser.add_argument("--current-leg-lot", type=float, default=0.19)
    parser.add_argument(
        "--output",
        default="data_vantage/phoenix_execution_fix_compare_60sessions_20260814.json",
    )
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(ROOT / env_file, override=True)
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(args.broker_offset_hours)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        probe_start = end - timedelta(days=max(130, int(args.sessions * 2.2)))
        rates = _rates(symbol, probe_start, end + timedelta(hours=1))
        session_dates, cutoff = _completed_sessions(rates, end, int(args.sessions))
        rates = rates[(rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))].reset_index(drop=True)
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, _ = await _fetch_history(rates, cutoff, session)
        signals = sorted(
            [item for item in signals if len(item.signal.entries) >= 2 and len(item.signal.tps) >= 1],
            key=lambda item: item.time,
        )
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate XAUUSD value for 0.01 lot")

        signal_ids = [int(item.message_id) for item in signals]
        variants: dict[str, Any] = {}
        for name in ("previous_near_no_extra_market", "fixed_cycle_plus_market"):
            trades, decisions = _replay_variant(
                variant=name,
                signals=signals,
                rates=rates,
                symbol=symbol,
                point=point,
                commission_per_001=float(args.commission_per_001),
                profit_per_usd_001=profit_per_usd_001,
                pending_minutes=int(args.pending_minutes),
                provider_sl_cap=float(args.provider_sl_cap),
            )
            summary = _summary(trades, signal_ids)
            variants[name] = {
                "summary_001_per_leg": summary,
                "pnl_at_current_constant_leg_lot": round(
                    float(summary["pnl_001"]) * float(args.current_leg_lot) / 0.01,
                    2,
                ),
                "decisions": decisions,
                "trades": trades,
            }

        old_pnl = float(variants["previous_near_no_extra_market"]["summary_001_per_leg"]["pnl_001"])
        new_pnl = float(variants["fixed_cycle_plus_market"]["summary_001_per_leg"]["pnl_001"])
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions_included": session_dates,
            "symbol": symbol,
            "signals": len(signals),
            "signal_ids": signal_ids,
            "assumptions": {
                "targets": [1, 5, 6],
                "pending_expiry_minutes": int(args.pending_minutes),
                "provider_sl_cap_usd": float(args.provider_sl_cap),
                "commission_per_001": float(args.commission_per_001),
                "dynamic_m1_spread": True,
                "same_bar_ordering": "SL first",
                "current_constant_leg_lot": float(args.current_leg_lot),
                "previous": "all three targets at nearest range edge; market TP1 replaced its pending when allowed",
                "fixed": "three targets distributed near/middle/deep plus separate market TP1 when fresh and before TP1",
            },
            "variants": variants,
            "difference": {
                "pnl_001": round(new_pnl - old_pnl, 2),
                "pnl_at_current_constant_leg_lot": round(
                    (new_pnl - old_pnl) * float(args.current_leg_lot) / 0.01,
                    2,
                ),
            },
        }
        out = ROOT / args.output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(out),
                    "range_utc": output["range_utc"],
                    "signals": len(signals),
                    "previous": variants["previous_near_no_extra_market"]["summary_001_per_leg"],
                    "fixed": variants["fixed_cycle_plus_market"]["summary_001_per_leg"],
                    "difference": output["difference"],
                },
                indent=2,
            )
        )
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
