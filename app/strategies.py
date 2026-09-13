from __future__ import annotations

import os
from dataclasses import asdict, dataclass

import pandas as pd

from .config import Settings
from .indicators import enrich


AUTONOMOUS_STRATEGIES = ("trend_pullback", "breakout_momentum", "mean_reversion")


def _enabled_autonomous_strategies() -> set[str]:
    raw = os.getenv("AUTONOMOUS_ENABLED_STRATEGIES", ",".join(AUTONOMOUS_STRATEGIES))
    requested = {value.strip().lower() for value in raw.split(",") if value.strip()}
    unknown = sorted(requested - set(AUTONOMOUS_STRATEGIES))
    if unknown:
        raise ValueError(f"Unknown autonomous strategies: {', '.join(unknown)}")
    return requested


@dataclass
class TradeSignal:
    strategy: str
    side: str
    score: float
    entry: float
    sl: float
    tp: float
    atr: float
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def _regime_bias(regime_df: pd.DataFrame) -> tuple[str, float, str]:
    r = enrich(regime_df)
    if len(r) < 220:
        return "flat", 0.0, "regime_not_ready"
    last = r.iloc[-1]
    trend_up = last["ema50"] > last["ema200"] and last["close"] > last["ema20"]
    trend_down = last["ema50"] < last["ema200"] and last["close"] < last["ema20"]
    adx_ok = float(last["adx14"]) >= 16.0
    if trend_up and adx_ok:
        return "buy", 70.0, "regime_bullish"
    if trend_down and adx_ok:
        return "sell", 70.0, "regime_bearish"
    return "flat", 45.0, "regime_neutral"


def _trend_pullback(df: pd.DataFrame, regime_side: str, cfg: Settings) -> TradeSignal | None:
    d = enrich(df)
    if len(d) < 220:
        return None
    last = d.iloc[-1]
    prev = d.iloc[-2]
    atr = float(last["atr14"] or 0.0)
    if atr <= 0:
        return None
    long_setup = (
        regime_side == "buy"
        and prev["close"] <= prev["ema20"]
        and last["close"] > last["ema20"]
        and last["rsi14"] > 52
        and last["adx14"] >= 16
    )
    short_setup = (
        regime_side == "sell"
        and prev["close"] >= prev["ema20"]
        and last["close"] < last["ema20"]
        and last["rsi14"] < 48
        and last["adx14"] >= 16
    )
    if long_setup:
        entry = float(last["close"])
        sl = entry - atr * cfg.trend_sl_atr
        tp = entry + (entry - sl) * cfg.trend_tp_rr
        return TradeSignal("trend_pullback", "buy", 74.0, entry, sl, tp, atr, "pullback_to_ema20_in_uptrend")
    if short_setup:
        entry = float(last["close"])
        sl = entry + atr * cfg.trend_sl_atr
        tp = entry - (sl - entry) * cfg.trend_tp_rr
        return TradeSignal("trend_pullback", "sell", 74.0, entry, sl, tp, atr, "pullback_to_ema20_in_downtrend")
    return None


def _breakout(df: pd.DataFrame, regime_side: str, cfg: Settings) -> TradeSignal | None:
    d = enrich(df)
    if len(d) < 120:
        return None
    last = d.iloc[-1]
    atr = float(last["atr14"] or 0.0)
    vol_ok = float(last["tick_volume"] or 0.0) > float(last["volume_ma20"] or 0.0) * 1.15
    long_setup = (
        regime_side == "buy"
        and last["close"] > last["hh20"]
        and last["adx14"] >= 18
        and last["rsi14"] >= 58
        and vol_ok
    )
    short_setup = (
        regime_side == "sell"
        and last["close"] < last["ll20"]
        and last["adx14"] >= 18
        and last["rsi14"] <= 42
        and vol_ok
    )
    if atr <= 0:
        return None
    if long_setup:
        entry = float(last["close"])
        sl = entry - atr * cfg.breakout_sl_atr
        tp = entry + (entry - sl) * cfg.breakout_tp_rr
        return TradeSignal("breakout_momentum", "buy", 82.0, entry, sl, tp, atr, "20_bar_breakout_with_volume")
    if short_setup:
        entry = float(last["close"])
        sl = entry + atr * cfg.breakout_sl_atr
        tp = entry - (sl - entry) * cfg.breakout_tp_rr
        return TradeSignal("breakout_momentum", "sell", 82.0, entry, sl, tp, atr, "20_bar_breakdown_with_volume")
    return None


def _mean_reversion(df: pd.DataFrame, regime_side: str, cfg: Settings) -> TradeSignal | None:
    d = enrich(df)
    if len(d) < 120:
        return None
    last = d.iloc[-1]
    atr = float(last["atr14"] or 0.0)
    if atr <= 0:
        return None
    range_mode = 10.0 <= float(last["adx14"] or 0.0) <= 18.0
    long_setup = (
        regime_side == "flat"
        and range_mode
        and last["close"] <= last["bb_lower"]
        and last["rsi14"] <= 33
    )
    short_setup = (
        regime_side == "flat"
        and range_mode
        and last["close"] >= last["bb_upper"]
        and last["rsi14"] >= 67
    )
    if long_setup:
        entry = float(last["close"])
        sl = entry - atr * cfg.meanrev_sl_atr
        tp = entry + (entry - sl) * cfg.meanrev_tp_rr
        return TradeSignal("mean_reversion", "buy", 65.0, entry, sl, tp, atr, "bollinger_lower_reversal")
    if short_setup:
        entry = float(last["close"])
        sl = entry + atr * cfg.meanrev_sl_atr
        tp = entry - (sl - entry) * cfg.meanrev_tp_rr
        return TradeSignal("mean_reversion", "sell", 65.0, entry, sl, tp, atr, "bollinger_upper_reversal")
    return None


def generate_signal(entry_df: pd.DataFrame, regime_df: pd.DataFrame, cfg: Settings) -> tuple[TradeSignal | None, dict]:
    regime_side, regime_score, regime_reason = _regime_bias(regime_df)
    enabled = _enabled_autonomous_strategies()
    factories = (
        ("trend_pullback", _trend_pullback),
        ("breakout_momentum", _breakout),
        ("mean_reversion", _mean_reversion),
    )
    candidates = [factory(entry_df, regime_side, cfg) for name, factory in factories if name in enabled]
    candidates = [candidate for candidate in candidates if candidate is not None]
    candidates.sort(key=lambda item: item.score, reverse=True)
    best = candidates[0] if candidates else None
    if best is not None:
        if best.side == "buy" and not cfg.enable_longs:
            best = None
        if best is not None and best.side == "sell" and not cfg.enable_shorts:
            best = None
    diagnostics = {
        "regime_side": regime_side,
        "regime_score": regime_score,
        "regime_reason": regime_reason,
        "enabled_strategies": sorted(enabled),
        "candidates": [candidate.as_dict() for candidate in candidates],
    }
    if best is not None and best.score < cfg.min_signal_score:
        diagnostics["filtered_out"] = "score_below_threshold"
        return None, diagnostics
    return best, diagnostics
