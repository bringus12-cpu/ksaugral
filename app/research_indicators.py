"""Additional causal indicators for offline strategy research."""
import numpy as np
import pandas as pd

from .indicators import atr


def demarker(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up = frame.high.diff().clip(lower=0).rolling(period).mean()
    down = (-frame.low.diff()).clip(lower=0).rolling(period).mean()
    return up / (up + down).replace(0, np.nan)


def vortex(frame: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series]:
    tr = pd.concat([frame.high - frame.low, (frame.high - frame.close.shift()).abs(),
                    (frame.low - frame.close.shift()).abs()], axis=1).max(axis=1)
    denominator = tr.rolling(period).sum().replace(0, np.nan)
    return ((frame.high - frame.low.shift()).abs().rolling(period).sum() / denominator,
            (frame.low - frame.high.shift()).abs().rolling(period).sum() / denominator)


def chandelier(frame: pd.DataFrame, period: int = 22, multiplier: float = 3.0) -> tuple[pd.Series, pd.Series]:
    distance = atr(frame, period) * multiplier
    return frame.high.rolling(period).max() - distance, frame.low.rolling(period).min() + distance
