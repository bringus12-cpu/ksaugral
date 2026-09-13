from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_phoenix_active_profile import _completed_sessions
from scripts.backtest_phoenix_complete_60d import _fetch_history, _rates
from scripts.optimize_phoenix_copy_execution import (
    Candidate,
    _channel_cancel_times,
    _run_candidate,
    _summary,
)


CANDIDATES = {
    "near30_none": Candidate("near", 30, 12.0, "none", False),
    "near15_none": Candidate("near", 15, 12.0, "none", False),
    "near30_fast_be": Candidate("near", 30, 12.0, "fast_be", False),
    "near15_fast_be": Candidate("near", 15, 12.0, "fast_be", False),
    "near30_delayed": Candidate("near", 30, 12.0, "delayed", False),
    "near30_deep_ladder": Candidate("near", 30, 12.0, "deep_ladder", False),
    "near30_cap8_none": Candidate("near", 30, 8.0, "none", False),
    "near30_provider_sl_none": Candidate("near", 30, 0.0, "none", False),
    "near30_none_cancel": Candidate("near", 30, 12.0, "none", True),
    "middle30_none": Candidate("middle", 30, 12.0, "none", False),
    "cycle30_none": Candidate("cycle", 30, 12.0, "none", False),
    "side_deep30_none": Candidate("side_deep", 30, 12.0, "none", False),
}


def _target_stats(trades: list[dict[str, Any]], signal_ids: list[int]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for target in range(1, 7):
        rows = [row for row in trades if int(row["target_index"]) == target]
        stats = _summary(rows, signal_ids)
        stats["target_hits"] = sum(str(row.get("status")) == "target" for row in rows)
        stats["target_hit_rate_pct"] = round(
            100.0 * stats["target_hits"] / max(1, len(rows)), 2
        )
        output[f"tp{target}"] = stats
    return output


def _plan_rows(trades: list[dict[str, Any]], plan: tuple[int, int, int]) -> list[dict[str, Any]]:
    by_target: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_target[int(row["target_index"])].append(row)
    output: list[dict[str, Any]] = []
    for target in plan:
        output.extend(dict(row) for row in by_target[target])
    return output


def _plans(
    trades: list[dict[str, Any]],
    signal_ids: list[int],
    train_ids: list[int],
    holdout_ids: list[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plan in itertools.combinations_with_replacement(range(1, 7), 3):
        selected = _plan_rows(trades, plan)
        train = _summary(selected, train_ids)
        holdout = _summary(selected, holdout_ids)
        full = _summary(selected, signal_ids)
        rows.append(
            {
                "targets": list(plan),
                "robust_positive": train["pnl_001"] > 0.0 and holdout["pnl_001"] > 0.0,
                "train": train,
                "holdout": holdout,
                "full": full,
            }
        )
    rows.sort(
        key=lambda row: (
            bool(row["robust_positive"]),
            min(float(row["train"]["pnl_001"]), float(row["holdout"]["pnl_001"])),
            float(row["holdout"]["signal_win_rate_pct"]),
            float(row["full"]["pnl_001"]),
        ),
        reverse=True,
    )
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=120)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument(
        "--output",
        default="data_vantage/phoenix_practical_strategy_120sessions.json",
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
        rates = _rates(
            symbol,
            end - timedelta(days=max(230, int(args.sessions * 2.0))),
            end + timedelta(hours=1),
        )
        session_dates, cutoff = _completed_sessions(rates, end, int(args.sessions))
        rates = rates[
            (rates["time"] >= pd.Timestamp(cutoff)) & (rates["time"] < pd.Timestamp(end))
        ].reset_index(drop=True)
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, _ = await _fetch_history(rates, cutoff, session)
        signals = sorted(
            [item for item in signals if len(item.signal.entries) >= 2 and item.signal.tps],
            key=lambda item: item.time,
        )
        signal_ids = [int(item.message_id) for item in signals]
        split = max(1, int(len(signal_ids) * 0.60))
        train_ids = signal_ids[:split]
        holdout_ids = signal_ids[split:]
        broker_offset = timedelta(hours=float(args.broker_offset_hours))
        cancel_times = await _channel_cancel_times(cfg, cutoff, broker_offset)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(rates.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate XAUUSD value per USD at 0.01 lot")

        variants: dict[str, Any] = {}
        for name, candidate in CANDIDATES.items():
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
            plans = _plans(trades, signal_ids, train_ids, holdout_ids)
            variants[name] = {
                "candidate": candidate.__dict__,
                "train": _summary(trades, train_ids),
                "holdout": _summary(trades, holdout_ids),
                "full": _summary(trades, signal_ids),
                "by_target": _target_stats(trades, signal_ids),
                "top_three_leg_plans": plans[:12],
            }

        ranking = [
            {
                "variant": name,
                "candidate": row["candidate"],
                "train": row["train"],
                "holdout": row["holdout"],
                "full": row["full"],
                "best_three_leg_plan": row["top_three_leg_plans"][0],
            }
            for name, row in variants.items()
        ]
        ranking.sort(
            key=lambda row: (
                row["train"]["pnl_001"] > 0.0 and row["holdout"]["pnl_001"] > 0.0,
                min(float(row["train"]["pnl_001"]), float(row["holdout"]["pnl_001"])),
                float(row["holdout"]["signal_win_rate_pct"]),
                float(row["full"]["pnl_001"]),
            ),
            reverse=True,
        )
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
            "sessions_included": session_dates,
            "signals": len(signal_ids),
            "train_signals": len(train_ids),
            "holdout_signals": len(holdout_ids),
            "method": (
                "Phoenix full-signal replay on broker M1 candles with dynamic spread, commission and SL-first "
                "ambiguous bars; 60/40 chronological split; practical entry, expiry, SL-cap and protection variants."
            ),
            "ranking": ranking,
            "variants": variants,
            "limitations": [
                "The candidate family was chosen after prior research on part of this history.",
                "Closed-bar M1 replay cannot reproduce tick-order, latency or slippage exactly.",
                "Target hit rate is measured after a pending order was filled, not across every posted Telegram signal.",
            ],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(out),
                    "range_utc": output["range_utc"],
                    "signals": output["signals"],
                    "ranking": ranking,
                },
                indent=2,
            )
        )
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
