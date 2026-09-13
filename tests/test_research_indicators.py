import numpy as np
import pandas as pd

from app.research_indicators import chandelier, demarker, vortex


def _frame(rows: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 2000 + np.cumsum(rng.normal(0, 2, rows))
    spread = rng.uniform(0.5, 3.0, rows)
    return pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.3, rows),
            "high": close + spread,
            "low": close - spread,
            "close": close,
        }
    )


def test_research_indicators_are_causal() -> None:
    full = _frame()
    truncated = full.iloc[:60].copy()

    calculations = (
        (demarker(full), demarker(truncated)),
        *zip(vortex(full), vortex(truncated)),
        *zip(chandelier(full), chandelier(truncated)),
    )
    for complete, partial in calculations:
        pd.testing.assert_series_equal(
            complete.iloc[:60], partial, check_names=False, check_exact=False
        )


def test_demarker_is_normalized() -> None:
    values = demarker(_frame()).dropna()
    assert values.between(0, 1).all()
