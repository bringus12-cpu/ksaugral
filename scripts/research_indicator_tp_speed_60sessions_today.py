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
from scripts.research_bollinger_macd_hybrids_60sessions import EXITS, PRESETS, _prepare, _signals, _simulate
from scripts.research_indicator_sl_width_60sessions_today import DEFINITIONS


SELECTED_SL_MULTIPLIERS = {
    "IND-BB-KELT-MACD": 1.0,
    "IND-VWAP-MACD": 1.25,
    "IND-BB-MACD-RCL": 1.5,
    "IND-RSI-MACD": 1.0,
    "IND-BB-MACD-BRK": 1.75,
    "IND-STOCH-BB": 1.0,
}
TP_MULTIPLIERS = (0.10, 0.20, 0.30, 0.40, 0.50, 0.65, 0.75, 0.85, 1.0)


def _run_window(
    frame: pd.DataFrame,
    buy,
    sell,
    definition: dict[str, str | int],
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--validation-sessions", type=int, default=20)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/indicator_tp_speed_60sessions_today.json")
    args = parser.parse_args()

    sessions = max(30, int(args.sessions))
    validation_sessions = max(10, min(sessions - 10, int(args.validation_sessions)))
    load_dotenv(Path(args.env).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        now = datetime.now(UTC)
        midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        fetch_start = midnight - timedelta(days=max(125, int(sessions * 2.0)))
        m1 = _rates(symbol, "M1", fetch_start - timedelta(days=5), now + timedelta(minutes=1))
        m5 = _rates(symbol, "M5", fetch_start - timedelta(days=5), now + timedelta(minutes=5))
        session_key = (m1["time"] + pd.Timedelta(hours=2)).dt.date
        counts = m1.loc[m1["time"] < pd.Timestamp(midnight)].groupby(session_key).size()
        completed = [day for day, count in counts.items() if int(count) >= 180]
        if len(completed) < sessions:
            raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {sessions}")
        selected = completed[-sessions:]
        validation_days = selected[-validation_sessions:]
        selected_mask = session_key.isin(selected)
        validation_mask = session_key.isin(validation_days)
        full_start = m1.loc[selected_mask, "time"].iloc[0].to_pydatetime()
        validation_start = m1.loc[validation_mask, "time"].iloc[0].to_pydatetime()
        today_key = (pd.Timestamp(now) + pd.Timedelta(hours=2)).date()
        today_mask = session_key == today_key
        today_start = m1.loc[today_mask, "time"].iloc[0].to_pydatetime() if bool(today_mask.any()) else now

        info = mt5.symbol_info(symbol)
        point = float(getattr(info, "point", 0.01) or 0.01)
        anchor = float(m1.iloc[-1]["close"])
        profit_per_usd_001 = abs(float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0))
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate XAUUSD value for 0.01 lot")

        prepared: dict[tuple[str, str], pd.DataFrame] = {}
        results: list[dict[str, Any]] = []
        for strategy, definition in DEFINITIONS.items():
            timeframe = str(definition["timeframe"])
            preset_name = str(definition["preset"])
            key = (timeframe, preset_name)
            if key not in prepared:
                prepared[key] = _prepare(m1 if timeframe == "M1" else m5, PRESETS[preset_name])
            frame = prepared[key]
            buy, sell = _signals(frame, str(definition["family"]), PRESETS[preset_name])
            sl_multiplier = float(SELECTED_SL_MULTIPLIERS[strategy])
            be_r = float(EXITS[str(definition["exit"])]["be_r"])
            for tp_multiplier in TP_MULTIPLIERS:
                be_modes = (("unchanged", 1.0),)
                if be_r > 0.0 and tp_multiplier < 1.0:
                    be_modes += (("proportional", tp_multiplier),)
                for be_mode, be_multiplier in be_modes:
                    results.append(
                        {
                            "strategy": strategy,
                            "definition": definition,
                            "sl_multiplier": sl_multiplier,
                            "tp_multiplier": tp_multiplier,
                            "be_mode": be_mode,
                            "be_multiplier": be_multiplier,
                            "full_60_sessions_plus_today": _run_window(
                                frame, buy, sell, definition, m1, full_start, now, point, profit_per_usd_001,
                                float(args.commission_per_001), sl_multiplier, tp_multiplier, be_multiplier,
                            ),
                            "validation_20_sessions_plus_today": _run_window(
                                frame, buy, sell, definition, m1, validation_start, now, point, profit_per_usd_001,
                                float(args.commission_per_001), sl_multiplier, tp_multiplier, be_multiplier,
                            ),
                            "today_partial": _run_window(
                                frame, buy, sell, definition, m1, today_start, now, point, profit_per_usd_001,
                                float(args.commission_per_001), sl_multiplier, tp_multiplier, be_multiplier,
                            ),
                        }
                    )

        output = {
            "generated_utc": now.isoformat(),
            "symbol": symbol,
            "range_utc": {"start": full_start.isoformat(), "validation_start": validation_start.isoformat(), "end": now.isoformat()},
            "completed_sessions": [day.isoformat() for day in selected],
            "validation_sessions": [day.isoformat() for day in validation_days],
            "today_partial_session": today_key.isoformat(),
            "lot_per_trade": 0.01,
            "selected_sl_multipliers": SELECTED_SL_MULTIPLIERS,
            "tp_multipliers": list(TP_MULTIPLIERS),
            "method": "Shorter TP only; SL uses selected module width. BE is tested unchanged and proportionally earlier. Dynamic M1 spread, 0.06 USD commission per 0.01 lot and conservative SL-first same-bar ordering.",
            "results": results,
        }
        out = ROOT / str(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(out)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
