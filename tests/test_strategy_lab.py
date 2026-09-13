from __future__ import annotations

import pandas as pd

from app.indicators import enrich
from app.strategy_lab import LAB_STRATEGY_DESCRIPTIONS, indicator_strategy_votes


def _frame(periods: int, frequency: str) -> pd.DataFrame:
    index = pd.RangeIndex(periods)
    close = pd.Series(100.0 + index * 0.02 + ((index % 13) - 6) * 0.07, dtype=float)
    return enrich(
        pd.DataFrame(
            {
                "time": pd.date_range("2026-01-01", periods=periods, freq=frequency, tz="UTC"),
                "open": close.shift(1).fillna(close.iloc[0]),
                "high": close + 0.20,
                "low": close - 0.20,
                "close": close,
                "tick_volume": 100 + index % 19,
            }
        )
    )


def test_strategy_lab_registers_twenty_distinct_indicator_strategies() -> None:
    assert len(LAB_STRATEGY_DESCRIPTIONS) == 20
    votes = indicator_strategy_votes(_frame(320, "1min"), _frame(320, "5min"), _frame(320, "15min"), 0.01)

    assert len(votes) == 20
    assert {name for name, _, _, _ in votes} == set(LAB_STRATEGY_DESCRIPTIONS)
    assert all(side in {"buy", "sell", "hold"} for _, side, _, _ in votes)


def test_strategy_lab_spread_veto_blocks_entries() -> None:
    votes = indicator_strategy_votes(_frame(320, "1min"), _frame(320, "5min"), _frame(320, "15min"), 10.0)

    assert all(side == "hold" for _, side, _, _ in votes)
