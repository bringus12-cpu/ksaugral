from __future__ import annotations

import pandas as pd

from app.indicators import enrich, macd


def test_macd_histogram_matches_main_minus_signal() -> None:
    close = pd.Series([float(value) for value in range(1, 80)])
    main, signal, histogram = macd(close)

    pd.testing.assert_series_equal(histogram, main - signal)
    assert float(main.iloc[-1]) > 0.0


def test_enrich_exposes_bollinger_and_macd_derivatives() -> None:
    close = pd.Series([100.0 + (index * 0.2) for index in range(240)])
    frame = pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "tick_volume": 100,
        }
    )

    enriched = enrich(frame)

    assert {
        "bb_width",
        "bb_percent_b",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_fast_upper",
        "macd_fast_hist",
        "bb_keltner_fast_squeeze",
        "bb_slow_percent_b",
        "macd_slow_hist",
        "stoch_k",
        "stoch_d",
        "vwap",
    }.issubset(enriched.columns)
    assert enriched[
        ["bb_width", "bb_percent_b", "macd_hist", "macd_fast_hist", "macd_slow_hist", "stoch_k", "vwap"]
    ].iloc[-1].notna().all()


def test_enrich_exposes_extended_indicator_laboratory() -> None:
    close = pd.Series([100.0 + index * 0.03 + ((index % 11) - 5) * 0.08 for index in range(320)])
    frame = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=len(close), freq="5min", tz="UTC"),
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.25,
            "low": close - 0.25,
            "close": close,
            "tick_volume": 100 + (close.index % 17),
        }
    )

    enriched = enrich(frame)
    required = {
        "wma20", "dema20", "tema20", "hma20", "kama10", "vwma20",
        "roc12", "cci20", "williams_r14", "mfi14", "obv", "cmf20",
        "adl", "adosc", "ppo", "cmo14", "trix15", "ultimate_osc",
        "awesome_osc", "aroon_osc", "dpo20", "natr14", "choppiness14",
        "efficiency10", "zscore20", "linreg_slope20", "fisher10",
        "supertrend", "supertrend_dir", "ichimoku_span_a", "ichimoku_span_b",
        "donchian_upper20", "force_index13", "mass_index25", "kst",
        "realized_vol20",
    }
    assert required <= set(enriched.columns)
    assert enriched[list(required)].iloc[-1].notna().all()


def test_extended_indicators_do_not_change_past_when_future_bars_change() -> None:
    close = pd.Series([100.0 + index * 0.02 + ((index % 13) - 6) * 0.06 for index in range(340)])
    frame = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=len(close), freq="5min", tz="UTC"),
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.30,
            "low": close - 0.30,
            "close": close,
            "tick_volume": 100 + (close.index % 19),
        }
    )
    baseline = enrich(frame)
    changed = frame.copy()
    changed.loc[320:, ["open", "high", "low", "close"]] += 500.0
    changed.loc[320:, "tick_volume"] *= 20
    recalculated = enrich(changed)
    derived = [column for column in baseline.columns if column not in frame.columns]

    pd.testing.assert_frame_equal(
        baseline.loc[:319, derived],
        recalculated.loc[:319, derived],
        check_exact=True,
    )
