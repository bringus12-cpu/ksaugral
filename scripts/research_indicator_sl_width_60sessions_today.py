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


DEFINITIONS: dict[str, dict[str, str | int]] = {
    "IND-BB-KELT-MACD": {"family": "bb_keltner_squeeze", "timeframe": "M5", "minutes": 5, "preset": "fast", "exit": "trend_runner"},
    "IND-VWAP-MACD": {"family": "vwap_macd_reclaim", "timeframe": "M5", "minutes": 5, "preset": "fast", "exit": "trend_runner"},
    "IND-BB-MACD-RCL": {"family": "bb_macd_reclaim", "timeframe": "M5", "minutes": 5, "preset": "slow", "exit": "quick_be"},
    "IND-RSI-MACD": {"family": "rsi_macd_reversal", "timeframe": "M5", "minutes": 5, "preset": "slow", "exit": "balanced"},
    "IND-BB-MACD-BRK": {"family": "bb_macd_breakout", "timeframe": "M5", "minutes": 5, "preset": "standard", "exit": "wide_no_be"},
    "IND-STOCH-BB": {"family": "stoch_bb_reversion", "timeframe": "M1", "minutes": 1, "preset": "standard", "exit": "wide_no_be"},
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument("--output", default="data_vantage/scalper_sl_width_indicator_60sessions_today.json")
    args = parser.parse_args()

    load_dotenv(Path(args.env).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        now = datetime.now(UTC)
        midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        fetch_start = midnight - timedelta(days=max(125, int(args.sessions * 2.0)))
        m1 = _rates(symbol, "M1", fetch_start - timedelta(days=5), now + timedelta(minutes=1))
        m5 = _rates(symbol, "M5", fetch_start - timedelta(days=5), now + timedelta(minutes=5))
        session_key = (m1["time"] + pd.Timedelta(hours=2)).dt.date
        counts = m1.loc[m1["time"] < pd.Timestamp(midnight)].groupby(session_key).size()
        completed = [day for day, count in counts.items() if int(count) >= 180]
        if len(completed) < int(args.sessions):
            raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {args.sessions}")
        selected = completed[-int(args.sessions):]
        selected_mask = session_key.isin(selected)
        full_start = m1.loc[selected_mask, "time"].iloc[0].to_pydatetime()
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
        multipliers = (1.0, 1.25, 1.5, 1.75, 2.0)
        for strategy, definition in DEFINITIONS.items():
            timeframe = str(definition["timeframe"])
            preset_name = str(definition["preset"])
            key = (timeframe, preset_name)
            if key not in prepared:
                prepared[key] = _prepare(m1 if timeframe == "M1" else m5, PRESETS[preset_name])
            frame = prepared[key]
            buy, sell = _signals(frame, str(definition["family"]), PRESETS[preset_name])
            for multiplier in multipliers:
                full, _ = _simulate(
                    frame,
                    buy,
                    sell,
                    int(definition["minutes"]),
                    EXITS[str(definition["exit"])],
                    m1,
                    full_start,
                    now,
                    point,
                    profit_per_usd_001,
                    float(args.commission_per_001),
                    sl_distance_multiplier=multiplier,
                )
                today, _ = _simulate(
                    frame,
                    buy,
                    sell,
                    int(definition["minutes"]),
                    EXITS[str(definition["exit"])],
                    m1,
                    today_start,
                    now,
                    point,
                    profit_per_usd_001,
                    float(args.commission_per_001),
                    sl_distance_multiplier=multiplier,
                )
                results.append(
                    {
                        "strategy": strategy,
                        "definition": definition,
                        "sl_multiplier": multiplier,
                        "full_60_sessions_plus_today": full,
                        "today_partial": today,
                    }
                )

        output = {
            "generated_utc": now.isoformat(),
            "symbol": symbol,
            "range_utc": {"start": full_start.isoformat(), "end": now.isoformat()},
            "completed_sessions": [day.isoformat() for day in selected],
            "today_partial_session": today_key.isoformat(),
            "lot_per_trade": 0.01,
            "sl_multipliers": list(multipliers),
            "method": "Only initial SL is widened. TP and BE trigger remain at baseline. Dynamic M1 spread, 0.06 USD commission per 0.01 lot and conservative SL-first same-bar ordering.",
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
