from __future__ import annotations

import argparse
import json
import sys
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
from scripts.backtest_xau_scalper_current import _rates
from scripts.research_bollinger_macd_hybrids_60sessions import EXITS, PRESETS, _prepare, _signals, _simulate
from scripts.research_indicator_sl_width_60sessions_today import DEFINITIONS
from scripts.research_indicator_tp_speed_60sessions_today import SELECTED_SL_MULTIPLIERS, TP_MULTIPLIERS


def _run(
    frame: pd.DataFrame,
    buy: np.ndarray,
    sell: np.ndarray,
    definition: dict[str, Any],
    m1: pd.DataFrame,
    start: datetime,
    end: datetime,
    point: float,
    profit_per_usd_001: float,
    commission_per_001: float,
    sl_multiplier: float,
    tp_multiplier: float,
    be_multiplier: float,
) -> dict[str, Any]:
    summary, _ = _simulate(
        frame,
        buy,
        sell,
        int(definition["minutes"]),
        EXITS[str(definition["exit"])],
        m1,
        start,
        end,
        point,
        profit_per_usd_001,
        commission_per_001,
        sl_distance_multiplier=sl_multiplier,
        tp_distance_multiplier=tp_multiplier,
        be_trigger_multiplier=be_multiplier,
    )
    return summary


def _run_with_trades(
    frame: pd.DataFrame,
    buy: np.ndarray,
    sell: np.ndarray,
    definition: dict[str, Any],
    m1: pd.DataFrame,
    start: datetime,
    end: datetime,
    point: float,
    profit_per_usd_001: float,
    commission_per_001: float,
    sl_multiplier: float,
    tp_multiplier: float,
    be_multiplier: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _simulate(
        frame,
        buy,
        sell,
        int(definition["minutes"]),
        EXITS[str(definition["exit"])],
        m1,
        start,
        end,
        point,
        profit_per_usd_001,
        commission_per_001,
        keep_trades=True,
        sl_distance_multiplier=sl_multiplier,
        tp_distance_multiplier=tp_multiplier,
        be_trigger_multiplier=be_multiplier,
    )


def _candidate_grid(strategy: str, definition: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sl_multiplier = float(SELECTED_SL_MULTIPLIERS[strategy])
    be_r = float(EXITS[str(definition["exit"])]["be_r"])
    for tp_multiplier in TP_MULTIPLIERS:
        be_modes = (("unchanged", 1.0),)
        if be_r > 0.0 and tp_multiplier < 1.0:
            be_modes += (("proportional", float(tp_multiplier)),)
        for be_mode, be_multiplier in be_modes:
            rows.append(
                {
                    "sl_multiplier": sl_multiplier,
                    "tp_multiplier": float(tp_multiplier),
                    "be_mode": be_mode,
                    "be_multiplier": float(be_multiplier),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--development-sessions", type=int, default=180)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/research_non_phoenix_indicator_hours_300s.json")
    args = parser.parse_args()

    load_dotenv(Path(args.env).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        sessions = max(60, int(args.sessions))
        development_sessions = max(60, min(sessions - 30, int(args.development_sessions)))
        midnight = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        fetch_start = midnight - timedelta(days=max(500, int(sessions * 2.0)))
        m1 = _rates(symbol, "M1", fetch_start - timedelta(days=5), midnight)
        m5 = _rates(symbol, "M5", fetch_start - timedelta(days=5), midnight)
        session_key = (m1["time"] + pd.Timedelta(hours=2)).dt.date
        counts = m1.loc[m1["time"] < pd.Timestamp(midnight)].groupby(session_key).size()
        completed = [day for day, count in counts.items() if int(count) >= 180]
        if len(completed) < sessions:
            raise RuntimeError(f"Only {len(completed)} sessions available; requested {sessions}")
        selected = completed[-sessions:]
        development = selected[:development_sessions]
        holdout = selected[development_sessions:]
        selected_mask = session_key.isin(selected)
        development_mask = session_key.isin(development)
        holdout_mask = session_key.isin(holdout)
        full_start = m1.loc[selected_mask, "time"].iloc[0].to_pydatetime()
        development_start = m1.loc[development_mask, "time"].iloc[0].to_pydatetime()
        holdout_start = m1.loc[holdout_mask, "time"].iloc[0].to_pydatetime()
        end = m1.loc[selected_mask, "time"].iloc[-1].to_pydatetime() + timedelta(minutes=1)

        fold_size = max(1, development_sessions // 3)
        fold_days = [development[index : index + fold_size] for index in range(0, development_sessions, fold_size)]
        fold_ranges: list[tuple[datetime, datetime]] = []
        for days in fold_days:
            mask = session_key.isin(days)
            fold_ranges.append(
                (
                    m1.loc[mask, "time"].iloc[0].to_pydatetime(),
                    m1.loc[mask, "time"].iloc[-1].to_pydatetime() + timedelta(minutes=1),
                )
            )

        info = mt5.symbol_info(symbol)
        point = float(getattr(info, "point", 0.01) or 0.01)
        anchor = float(m1.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate XAUUSD value for 0.01 lot")

        prepared: dict[tuple[str, str], pd.DataFrame] = {}
        finalists: list[dict[str, Any]] = []
        all_development: list[dict[str, Any]] = []
        for strategy, definition in DEFINITIONS.items():
            timeframe = str(definition["timeframe"])
            preset_name = str(definition["preset"])
            prep_key = (timeframe, preset_name)
            if prep_key not in prepared:
                prepared[prep_key] = _prepare(m1 if timeframe == "M1" else m5, PRESETS[preset_name])
            frame = prepared[prep_key]
            base_buy, base_sell = _signals(frame, str(definition["family"]), PRESETS[preset_name])
            entry_hours = (frame["time"] + pd.Timedelta(minutes=int(definition["minutes"]))).dt.hour.to_numpy()

            strategy_candidates: list[dict[str, Any]] = []
            for candidate in _candidate_grid(strategy, definition):
                stable_hours: list[int] = []
                hour_diagnostics: dict[str, Any] = {}
                for hour in range(24):
                    mask = entry_hours == hour
                    buy = base_buy & mask
                    sell = base_sell & mask
                    folds = [
                        _run(
                            frame,
                            buy,
                            sell,
                            definition,
                            m1,
                            fold_start,
                            fold_end,
                            point,
                            profit_per_usd_001,
                            float(args.commission_per_001),
                            float(candidate["sl_multiplier"]),
                            float(candidate["tp_multiplier"]),
                            float(candidate["be_multiplier"]),
                        )
                        for fold_start, fold_end in fold_ranges
                    ]
                    total_trades = sum(int(item["trades"]) for item in folds)
                    positive_folds = sum(float(item["pnl_001"]) > 0.0 for item in folds)
                    fold_pnl = round(sum(float(item["pnl_001"]) for item in folds), 2)
                    development_hour = _run(
                        frame,
                        buy,
                        sell,
                        definition,
                        m1,
                        development_start,
                        holdout_start,
                        point,
                        profit_per_usd_001,
                        float(args.commission_per_001),
                        float(candidate["sl_multiplier"]),
                        float(candidate["tp_multiplier"]),
                        float(candidate["be_multiplier"]),
                    )
                    stable = bool(
                        total_trades >= 30
                        and positive_folds >= 2
                        and fold_pnl > 0.0
                        and float(development_hour["pnl_001"]) > 0.0
                        and float(development_hour.get("profit_factor") or 0.0) >= 1.05
                    )
                    if stable:
                        stable_hours.append(hour)
                    hour_diagnostics[str(hour)] = {
                        "stable": stable,
                        "fold_pnl": [float(item["pnl_001"]) for item in folds],
                        "fold_trades": [int(item["trades"]) for item in folds],
                        "development": development_hour,
                    }

                if not stable_hours:
                    continue
                selected_mask_hours = np.isin(entry_hours, stable_hours)
                selected_buy = base_buy & selected_mask_hours
                selected_sell = base_sell & selected_mask_hours
                development_result = _run(
                    frame,
                    selected_buy,
                    selected_sell,
                    definition,
                    m1,
                    development_start,
                    holdout_start,
                    point,
                    profit_per_usd_001,
                    float(args.commission_per_001),
                    float(candidate["sl_multiplier"]),
                    float(candidate["tp_multiplier"]),
                    float(candidate["be_multiplier"]),
                )
                row = {
                    "strategy": strategy,
                    "definition": definition,
                    **candidate,
                    "selected_utc_hours": stable_hours,
                    "development": development_result,
                    "hour_diagnostics": hour_diagnostics,
                }
                strategy_candidates.append(row)
                all_development.append(row)

            eligible = [
                row
                for row in strategy_candidates
                if int(row["development"]["trades"]) >= 100
                and float(row["development"].get("profit_factor") or 0.0) >= 1.10
            ]
            if not eligible:
                continue
            winner = max(
                eligible,
                key=lambda row: (
                    float(row["development"].get("profit_to_drawdown") or -999.0),
                    float(row["development"]["pnl_001"]),
                ),
            )
            selected_mask_hours = np.isin(entry_hours, winner["selected_utc_hours"])
            selected_buy = base_buy & selected_mask_hours
            selected_sell = base_sell & selected_mask_hours
            winner = dict(winner)
            winner["holdout"] = _run(
                frame,
                selected_buy,
                selected_sell,
                definition,
                m1,
                holdout_start,
                end,
                point,
                profit_per_usd_001,
                float(args.commission_per_001),
                float(winner["sl_multiplier"]),
                float(winner["tp_multiplier"]),
                float(winner["be_multiplier"]),
            )
            winner["full"], winner["trades"] = _run_with_trades(
                frame,
                selected_buy,
                selected_sell,
                definition,
                m1,
                full_start,
                end,
                point,
                profit_per_usd_001,
                float(args.commission_per_001),
                float(winner["sl_multiplier"]),
                float(winner["tp_multiplier"]),
                float(winner["be_multiplier"]),
            )
            winner["blocks_60_sessions"] = []
            for block_index in range(0, sessions, 60):
                block_days = selected[block_index : block_index + 60]
                block_mask = session_key.isin(block_days)
                block_start = m1.loc[block_mask, "time"].iloc[0].to_pydatetime()
                block_end = m1.loc[block_mask, "time"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
                winner["blocks_60_sessions"].append(
                    {
                        "first_session": block_days[0].isoformat(),
                        "last_session": block_days[-1].isoformat(),
                        "result": _run(
                            frame,
                            selected_buy,
                            selected_sell,
                            definition,
                            m1,
                            block_start,
                            block_end,
                            point,
                            profit_per_usd_001,
                            float(args.commission_per_001),
                            float(winner["sl_multiplier"]),
                            float(winner["tp_multiplier"]),
                            float(winner["be_multiplier"]),
                        ),
                    }
                )
            finalists.append(winner)

        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "method": (
                "Hours and exit parameters selected only on first 180 sessions. "
                "An hour must be positive in at least 2/3 development folds, have at least 30 trades, "
                "positive combined development PnL and PF >= 1.05. One candidate per strategy is then "
                "locked and evaluated on the untouched final 120 sessions. Dynamic M1 spread, "
                "0.06 USD commission and SL-first same-bar ordering."
            ),
            "sessions": sessions,
            "development_sessions": [day.isoformat() for day in development],
            "holdout_sessions": [day.isoformat() for day in holdout],
            "finalists": finalists,
            "development_candidates_with_stable_hours": len(all_development),
        }
        out = ROOT / str(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(out)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
