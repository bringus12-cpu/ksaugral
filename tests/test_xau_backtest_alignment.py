from __future__ import annotations

import pandas as pd
import unittest

from scripts.backtest_xau_scalper_current import _align_frames


def _frame(times: list[str], closes: list[float]) -> pd.DataFrame:
    rows = []
    for ts, close in zip(times, closes):
        rows.append(
            {
                "time": pd.Timestamp(ts),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "ema20": close,
                "ema50": close,
                "adx14": 20.0,
                "atr14": 1.0,
            }
        )
    return pd.DataFrame(rows)


class BacktestAlignmentTests(unittest.TestCase):
    def test_higher_timeframe_values_are_visible_only_after_bar_close(self) -> None:
        m1 = _frame(
            ["2026-01-01T10:04:00Z", "2026-01-01T10:05:00Z", "2026-01-01T10:14:00Z", "2026-01-01T10:15:00Z"],
            [1.0, 1.0, 1.0, 1.0],
        )
        m5 = _frame(["2026-01-01T10:00:00Z", "2026-01-01T10:05:00Z"], [10.0, 20.0])
        m15 = _frame(["2026-01-01T10:00:00Z", "2026-01-01T10:15:00Z"], [100.0, 200.0])

        aligned = _align_frames(m1, m5, m15)

        self.assertTrue(pd.isna(aligned.iloc[0]["m5_close"]))
        self.assertEqual(aligned.iloc[1]["m5_close"], 10.0)
        self.assertTrue(pd.isna(aligned.iloc[2]["m15_ema20"]))
        self.assertEqual(aligned.iloc[3]["m15_ema20"], 100.0)
