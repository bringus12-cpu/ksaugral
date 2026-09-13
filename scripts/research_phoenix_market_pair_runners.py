from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _phoenix_market_runner_allowed, _repair_tps_for_entry
from scripts.backtest_phoenix_complete_60d import _fetch_history, _rates
from scripts.optimize_phoenix_copy_execution import (
    Candidate,
    _entry_plan,
    _run_candidate,
    _simulate_leg,
    _summary,
)


PROTECT_MODES = ("none", "be_tp1", "be_tp2", "be_tp3", "ladder")
TARGET_PAIRS = ((2, 5), (2, 6))
LOT_MULTIPLIERS = (1.0, 2.0, 3.0, 5.0)
MARKET_GATES = ("strict_zone", "before_tp1")


def _scaled_trade(row: dict[str, Any], multiplier: float, label: str) -> dict[str, Any]:
    result = dict(row)
    result["pnl_001"] = round(float(row["pnl_001"]) * float(multiplier), 4)
    result["runner_multiplier"] = float(multiplier)
    result["runner_label"] = label
    return result


def _market_allowed(item, rates: pd.DataFrame, point: float, gate: str) -> bool:
    start_idx = int(item.start_idx)
    if start_idx < 0 or start_idx >= len(rates):
        return False
    entries = [float(value) for value in item.signal.entries if float(value or 0.0) > 0.0]
    if not entries:
        return False
    spread = float(rates.iloc[start_idx]["spread"]) * point
    bid = float(rates.iloc[start_idx]["open"])
    ask = bid + spread
    market = ask if str(item.signal.side) == "buy" else bid
    original_tps = [float(value) for value in item.signal.tps if float(value or 0.0) > 0.0]
    if not original_tps:
        return False
    before_tp1 = bid < original_tps[0] if str(item.signal.side) == "buy" else ask > original_tps[0]
    if gate == "before_tp1":
        return bool(before_tp1)
    if gate == "strict_zone":
        return bool(before_tp1 and _phoenix_market_runner_allowed(item.signal.side, entries, market, original_tps))
    raise ValueError(f"Unknown market gate: {gate}")


def _runner_trade(
    *,
    item,
    rates: pd.DataFrame,
    point: float,
    symbol: str,
    target_index: int,
    protect_mode: str,
    market_gate: str,
    sl_cap: float,
    commission_per_001: float,
    profit_per_usd_001: float,
) -> dict[str, Any] | None:
    if not _market_allowed(item, rates, point, market_gate):
        return None
    start_idx = int(item.start_idx)
    spread = float(rates.iloc[start_idx]["spread"]) * point
    bid = float(rates.iloc[start_idx]["open"])
    market = bid + spread if str(item.signal.side) == "buy" else bid
    repaired = _repair_tps_for_entry(
        item.signal.side,
        market,
        [float(value) for value in item.signal.tps if float(value or 0.0) > 0.0],
        max(int(target_index), 6),
    )
    if len(repaired) < int(target_index):
        return None
    times = rates["time"].to_numpy(dtype="datetime64[ns]")
    return _simulate_leg(
        rates=rates,
        times=times,
        opens=rates["open"].to_numpy(dtype=float),
        highs=rates["high"].to_numpy(dtype=float),
        lows=rates["low"].to_numpy(dtype=float),
        closes=rates["close"].to_numpy(dtype=float),
        spreads=rates["spread"].to_numpy(dtype=float),
        point=point,
        symbol=symbol,
        item=item,
        entry=market,
        target_index=int(target_index),
        protect_mode=protect_mode,
        pending_minutes=60,
        provider_sl_cap=float(sl_cap),
        cancel_time=None,
        commission_per_001=float(commission_per_001),
        profit_per_usd_001=float(profit_per_usd_001),
        market_runner_override=True,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trading-days", type=int, default=90)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--sl-cap", type=float, default=8.0)
    parser.add_argument("--output", default="data_vantage/research_phoenix_market_pair_runners_90sessions.json")
    args = parser.parse_args()

    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    os.environ["PHOENIX_BACKTEST_BROKER_OFFSET_HOURS"] = str(float(args.broker_offset_hours))
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        probe_start = end - timedelta(days=max(150, int(args.trading_days * 1.8)))
        rates = _rates(symbol, probe_start, end + timedelta(hours=1))
        sessions = sorted({timestamp.date() for timestamp in rates["time"] if int(timestamp.weekday()) < 5})
        if len(sessions) < int(args.trading_days):
            raise RuntimeError(f"Only {len(sessions)} sessions available")
        cutoff = datetime.combine(sessions[-int(args.trading_days)], datetime.min.time(), tzinfo=UTC)
        rates = rates[rates["time"] >= pd.Timestamp(cutoff)].reset_index(drop=True)
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
            raise RuntimeError("Could not calculate XAUUSD 0.01-lot value per USD")

        signal_ids = [int(item.message_id) for item in signals]
        split = max(1, int(len(signal_ids) * 0.60))
        train_ids, holdout_ids = signal_ids[:split], signal_ids[split:]

        baseline_candidate = Candidate("middle", 60, float(args.sl_cap), "none", False)
        baseline = _run_candidate(
            baseline_candidate,
            signals,
            rates,
            symbol,
            point,
            {},
            float(args.commission_per_001),
            profit_per_usd_001,
        )

        runner_cache: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
        for market_gate in MARKET_GATES:
            for target in sorted({target for pair in TARGET_PAIRS for target in pair}):
                for protect in PROTECT_MODES:
                    trades: list[dict[str, Any]] = []
                    for item in signals:
                        trade = _runner_trade(
                            item=item,
                            rates=rates,
                            point=point,
                            symbol=symbol,
                            target_index=target,
                            protect_mode=protect,
                            market_gate=market_gate,
                            sl_cap=float(args.sl_cap),
                            commission_per_001=float(args.commission_per_001),
                            profit_per_usd_001=profit_per_usd_001,
                        )
                        if trade is not None:
                            trades.append(trade)
                    runner_cache[(market_gate, target, protect)] = trades

        rows: list[dict[str, Any]] = []
        for market_gate in MARKET_GATES:
            for first_target, second_target in TARGET_PAIRS:
                for first_protect in PROTECT_MODES:
                    for second_protect in PROTECT_MODES:
                        for multiplier in LOT_MULTIPLIERS:
                            first = [
                                _scaled_trade(row, multiplier, f"TP{first_target}-{first_protect}")
                                for row in runner_cache[(market_gate, first_target, first_protect)]
                            ]
                            second = [
                                _scaled_trade(row, multiplier, f"TP{second_target}-{second_protect}")
                                for row in runner_cache[(market_gate, second_target, second_protect)]
                            ]
                            combined = list(baseline) + first + second
                            runner_only = first + second
                            rows.append(
                                {
                                    "market_gate": market_gate,
                                    "targets": [first_target, second_target],
                                    "protect": [first_protect, second_protect],
                                    "lot_multiplier_vs_001": multiplier,
                                    "runner_only": {
                                        "train": _summary(runner_only, train_ids),
                                        "holdout": _summary(runner_only, holdout_ids),
                                        "full": _summary(runner_only, signal_ids),
                                    },
                                    "baseline_plus_runners": {
                                        "train": _summary(combined, train_ids),
                                        "holdout": _summary(combined, holdout_ids),
                                        "full": _summary(combined, signal_ids),
                                    },
                                }
                            )

        rows.sort(
            key=lambda row: (
                row["runner_only"]["train"]["pnl_001"] > 0
                and row["runner_only"]["holdout"]["pnl_001"] > 0,
                min(
                    row["runner_only"]["train"]["pnl_001"],
                    row["runner_only"]["holdout"]["pnl_001"],
                ),
                row["runner_only"]["holdout"]["profit_factor"] or 0.0,
                row["runner_only"]["full"]["pnl_001"],
            ),
            reverse=True,
        )
        gate_signal_counts = {
            gate: sum(1 for item in signals if _market_allowed(item, rates, point, gate))
            for gate in MARKET_GATES
        }
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": cutoff.isoformat(), "end": end.isoformat(), "trading_days": int(args.trading_days)},
            "method": (
                "Vantage M1 bid candles; dynamic historical spread; commission included; SL-first on ambiguous bars; "
                "chronological 60/40 split; market runners only before original TP1 and inside the live Phoenix market gate"
            ),
            "symbol": symbol,
            "signals": len(signals),
            "market_gate_signal_counts": gate_signal_counts,
            "baseline_candidate": baseline_candidate.__dict__,
            "baseline": {
                "train": _summary(baseline, train_ids),
                "holdout": _summary(baseline, holdout_ids),
                "full": _summary(baseline, signal_ids),
            },
            "tested_combinations": len(rows),
            "top_combinations": rows[:25],
            "all_combinations": rows,
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(
            json.dumps(
                {
                    "range": output["range"],
                    "signals": output["signals"],
                    "market_gate_signal_counts": output["market_gate_signal_counts"],
                    "baseline": output["baseline"],
                    "tested_combinations": output["tested_combinations"],
                    "top_combinations": output["top_combinations"][:10],
                },
                indent=2,
            )
        )
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
