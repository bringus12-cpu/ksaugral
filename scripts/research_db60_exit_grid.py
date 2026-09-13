from __future__ import annotations

import argparse
import json
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
from scripts.backtest_xau_scalper_current import _rates
from scripts.research_bollinger_macd_hybrids_60sessions import (
    PRESETS,
    _prepare,
    _signals,
    _simulate,
)


SL_ATR_VALUES = (1.0, 1.5, 2.0, 2.5)
TP_R_VALUES = (0.8, 1.25, 1.8, 2.2, 2.8, 3.2)
BE_R_VALUES = (0.0, 0.5, 0.75, 1.0, 1.25)
HOLD_MINUTES_VALUES = (120, 240, 480)


def _candidate(sl_atr: float, tp_r: float, be_r: float, hold_minutes: int) -> dict[str, float | int]:
    return {
        "sl_atr": float(sl_atr),
        "tp_r": float(tp_r),
        "be_r": float(be_r),
        "be_buffer": 0.10 if be_r > 0.0 else 0.0,
        "hold_minutes": int(hold_minutes),
        "cooldown": 30,
    }


def _positive(result: dict[str, Any]) -> bool:
    return float(result.get("pnl_001", 0.0) or 0.0) > 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=90)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/research_db60_exit_grid_90sessions.json")
    args = parser.parse_args()

    load_dotenv(Path(args.env).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        fetch_start = end - timedelta(days=max(155, int(args.sessions * 1.8)))
        warmup = timedelta(days=5)
        m1 = _rates(symbol, "M1", fetch_start - warmup, end + timedelta(hours=1))
        m5 = _rates(symbol, "M5", fetch_start - warmup, end + timedelta(hours=1))
        session_key = (m1["time"] + pd.Timedelta(hours=2)).dt.date
        counts = m1.loc[m1["time"] < pd.Timestamp(end)].groupby(session_key).size()
        completed = [day for day, count in counts.items() if int(count) >= 180]
        if len(completed) < int(args.sessions):
            raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {args.sessions}")
        selected = completed[-int(args.sessions):]
        split = max(1, int(len(selected) * 2 / 3))
        train_sessions = selected[:split]
        test_sessions = selected[split:]
        selected_mask = session_key.isin(selected)
        train_mask = session_key.isin(train_sessions)
        test_mask = session_key.isin(test_sessions)
        full_start = m1.loc[selected_mask, "time"].iloc[0].to_pydatetime()
        train_start = m1.loc[train_mask, "time"].iloc[0].to_pydatetime()
        test_start = m1.loc[test_mask, "time"].iloc[0].to_pydatetime()
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(m1.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate 0.01-lot XAUUSD value")

        frame = _prepare(m5, PRESETS["standard"])
        buy, sell = _signals(frame, "bb_macd_breakout", PRESETS["standard"])
        rows: list[dict[str, Any]] = []
        for sl_atr in SL_ATR_VALUES:
            for tp_r in TP_R_VALUES:
                for be_r in BE_R_VALUES:
                    if be_r > 0.0 and be_r >= tp_r:
                        continue
                    for hold_minutes in HOLD_MINUTES_VALUES:
                        exit_cfg = _candidate(sl_atr, tp_r, be_r, hold_minutes)
                        train, _ = _simulate(
                            frame, buy, sell, 5, exit_cfg, m1, train_start, test_start,
                            point, profit_per_usd_001, float(args.commission_per_001),
                        )
                        test, _ = _simulate(
                            frame, buy, sell, 5, exit_cfg, m1, test_start, end,
                            point, profit_per_usd_001, float(args.commission_per_001),
                        )
                        full, _ = _simulate(
                            frame, buy, sell, 5, exit_cfg, m1, full_start, end,
                            point, profit_per_usd_001, float(args.commission_per_001),
                        )
                        rows.append(
                            {
                                "exit": exit_cfg,
                                "train": train,
                                "holdout": test,
                                "full": full,
                                "robust": _positive(train) and _positive(test),
                            }
                        )

        rows.sort(
            key=lambda row: (
                bool(row["robust"]),
                min(float(row["train"]["pnl_001"]), float(row["holdout"]["pnl_001"])),
                float(row["holdout"].get("profit_factor") or 0.0),
                float(row["full"]["pnl_001"]),
            ),
            reverse=True,
        )
        positive = [row for row in rows if _positive(row["full"])]
        robust = [row for row in rows if bool(row["robust"])]
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {
                "start": full_start.isoformat(),
                "split": test_start.isoformat(),
                "end": end.isoformat(),
                "sessions": len(selected),
                "train_sessions": len(train_sessions),
                "holdout_sessions": len(test_sessions),
            },
            "method": (
                "DB60 bb_macd_breakout M5 standard trigger; next-M1 market entry; dynamic historical spread; "
                "commission; SL-first ambiguous bars; fixed 0.01 lot; chronological development/holdout split"
            ),
            "tested_configurations": len(rows),
            "positive_full_count": len(positive),
            "robust_count": len(robust),
            "top_robust": robust[:30],
            "all_positive_full": positive,
            "all_results": rows,
            "limitation": (
                "This grid compares complete single-strategy simulations, but the best configurations still require "
                "a fresh full live-engine replay before activation because repeated testing creates selection bias."
            ),
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(
            json.dumps(
                {
                    "range": output["range"],
                    "tested": output["tested_configurations"],
                    "positive_full": output["positive_full_count"],
                    "robust": output["robust_count"],
                    "top": output["top_robust"][:10],
                },
                indent=2,
            )
        )
    finally:
        shutdown()


if __name__ == "__main__":
    main()
