from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.indicators import adx, atr, bollinger_bands, ema, macd, rsi
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from scripts.backtest_xau_scalper_current import _rates


FAMILIES: dict[str, str] = {
    "bb_macd_reclaim": "Powrot ceny do wnetrza Bollingera potwierdzony zwrotem histogramu MACD.",
    "bb_macd_breakout": "Wybicie z waskich pasm Bollingera przy rosnacym MACD i wolumenie.",
    "bb_macd_trend_pullback": "Pullback do srodka Bollingera w trendzie EMA20/EMA50 z ponownym impetem MACD.",
    "macd_adx_continuation": "Przejscie histogramu MACD przez zero tylko przy trendzie EMA i odpowiednim ADX.",
    "rsi_macd_reversal": "Wyjscie RSI ze skrajnosci potwierdzone poprawa MACD i polozeniem w pasmach.",
    "stoch_bb_reversion": "Przeciecie Stochastic w strefie skrajnej przy odrzuceniu zewnetrznego pasma.",
    "cci_macd_momentum": "Przebicie CCI +/-100 zgodne z trendem EMA i kierunkiem MACD.",
    "bb_keltner_squeeze": "Wyjscie Bollingera z kanalu Keltnera i wybicie ceny potwierdzone MACD.",
    "vwap_macd_reclaim": "Odzyskanie dziennego VWAP zgodne z EMA i narastajacym MACD.",
    "supertrend_macd_pullback": "Pullback do EMA20 zgodny z Supertrend i ponownym przyspieszeniem MACD.",
}


PRESETS: dict[str, dict[str, float | int]] = {
    "fast": {
        "bb_period": 14,
        "bb_dev": 1.8,
        "macd_fast": 8,
        "macd_slow": 21,
        "macd_signal": 5,
        "rsi_low": 35,
        "rsi_high": 65,
        "adx_min": 16,
        "keltner_mult": 1.4,
        "supertrend_mult": 2.4,
    },
    "standard": {
        "bb_period": 20,
        "bb_dev": 2.0,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "rsi_low": 32,
        "rsi_high": 68,
        "adx_min": 18,
        "keltner_mult": 1.5,
        "supertrend_mult": 2.8,
    },
    "slow": {
        "bb_period": 30,
        "bb_dev": 2.2,
        "macd_fast": 16,
        "macd_slow": 35,
        "macd_signal": 9,
        "rsi_low": 30,
        "rsi_high": 70,
        "adx_min": 20,
        "keltner_mult": 1.8,
        "supertrend_mult": 3.2,
    },
}


EXITS: dict[str, dict[str, float | int]] = {
    "quick_be": {"sl_atr": 1.0, "tp_r": 0.9, "be_r": 0.55, "be_buffer": 0.08, "hold_minutes": 120, "cooldown": 5},
    "balanced": {"sl_atr": 1.3, "tp_r": 1.25, "be_r": 0.75, "be_buffer": 0.10, "hold_minutes": 240, "cooldown": 10},
    "trend_runner": {"sl_atr": 1.6, "tp_r": 1.8, "be_r": 1.0, "be_buffer": 0.10, "hold_minutes": 360, "cooldown": 15},
    "wide_no_be": {"sl_atr": 2.0, "tp_r": 2.2, "be_r": 0.0, "be_buffer": 0.0, "hold_minutes": 480, "cooldown": 30},
}


def _cci(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    mean = typical.rolling(period).mean()
    deviation = typical.rolling(period).apply(lambda values: float(np.mean(np.abs(values - np.mean(values)))), raw=True)
    return (typical - mean) / (0.015 * deviation.replace(0.0, np.nan))


def _supertrend_direction(frame: pd.DataFrame, atr_series: pd.Series, multiplier: float) -> pd.Series:
    midpoint = (frame["high"] + frame["low"]) / 2.0
    basic_upper = midpoint + atr_series * multiplier
    basic_lower = midpoint - atr_series * multiplier
    close = frame["close"].to_numpy(dtype=float)
    upper = basic_upper.to_numpy(dtype=float)
    lower = basic_lower.to_numpy(dtype=float)
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = np.ones(len(frame), dtype=int)
    for index in range(1, len(frame)):
        if np.isnan(final_upper[index - 1]) or np.isnan(final_lower[index - 1]):
            continue
        if upper[index] < final_upper[index - 1] or close[index - 1] > final_upper[index - 1]:
            final_upper[index] = upper[index]
        else:
            final_upper[index] = final_upper[index - 1]
        if lower[index] > final_lower[index - 1] or close[index - 1] < final_lower[index - 1]:
            final_lower[index] = lower[index]
        else:
            final_lower[index] = final_lower[index - 1]
        if direction[index - 1] > 0:
            direction[index] = -1 if close[index] < final_lower[index] else 1
        else:
            direction[index] = 1 if close[index] > final_upper[index] else -1
    return pd.Series(direction, index=frame.index, dtype=int)


def _prepare(frame: pd.DataFrame, preset: dict[str, float | int]) -> pd.DataFrame:
    out = frame.copy().reset_index(drop=True)
    close = out["close"]
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["ema200"] = ema(close, 200)
    out["rsi14"] = rsi(close, 14)
    out["atr14"] = atr(out, 14)
    out["adx14"] = adx(out, 14)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = bollinger_bands(
        close,
        int(preset["bb_period"]),
        float(preset["bb_dev"]),
    )
    bb_span = (out["bb_upper"] - out["bb_lower"]).replace(0.0, np.nan)
    out["bb_width"] = bb_span / out["bb_mid"].abs().replace(0.0, np.nan)
    out["bb_width_median"] = out["bb_width"].rolling(100).median()
    out["bb_percent_b"] = (close - out["bb_lower"]) / bb_span
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(
        close,
        int(preset["macd_fast"]),
        int(preset["macd_slow"]),
        int(preset["macd_signal"]),
    )
    low14 = out["low"].rolling(14).min()
    high14 = out["high"].rolling(14).max()
    stochastic_span = (high14 - low14).replace(0.0, np.nan)
    out["stoch_k"] = 100.0 * (close - low14) / stochastic_span
    out["stoch_d"] = out["stoch_k"].rolling(3).mean()
    out["cci20"] = _cci(out, 20)
    out["volume_ma20"] = out["tick_volume"].rolling(20).mean()
    out["volume_ratio"] = out["tick_volume"] / out["volume_ma20"].replace(0.0, np.nan)
    keltner_mult = float(preset["keltner_mult"])
    out["keltner_upper"] = out["ema20"] + out["atr14"] * keltner_mult
    out["keltner_lower"] = out["ema20"] - out["atr14"] * keltner_mult
    out["squeeze"] = (out["bb_upper"] < out["keltner_upper"]) & (out["bb_lower"] > out["keltner_lower"])
    session_key = (out["time"] + pd.Timedelta(hours=2)).dt.date
    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    volume = out["tick_volume"].clip(lower=1.0)
    cumulative_value = (typical * volume).groupby(session_key).cumsum()
    cumulative_volume = volume.groupby(session_key).cumsum()
    out["vwap"] = cumulative_value / cumulative_volume.replace(0.0, np.nan)
    out["supertrend_dir"] = _supertrend_direction(out, out["atr14"], float(preset["supertrend_mult"]))
    candle_range = (out["high"] - out["low"]).replace(0.0, np.nan)
    out["close_location"] = (out["close"] - out["low"]) / candle_range
    return out


def _signals(frame: pd.DataFrame, family: str, preset: dict[str, float | int]) -> tuple[np.ndarray, np.ndarray]:
    prev = frame.shift(1)
    close = frame["close"]
    open_ = frame["open"]
    low = frame["low"]
    high = frame["high"]
    hist = frame["macd_hist"]
    prev_hist = prev["macd_hist"]
    trend_up = frame["ema20"] > frame["ema50"]
    trend_down = frame["ema20"] < frame["ema50"]
    rsi_low = float(preset["rsi_low"])
    rsi_high = float(preset["rsi_high"])
    adx_min = float(preset["adx_min"])

    if family == "bb_macd_reclaim":
        buy = (
            (low <= frame["bb_lower"])
            & (close > frame["bb_lower"])
            & (close > open_)
            & (hist > prev_hist)
            & (frame["rsi14"] <= rsi_low + 12.0)
            & (frame["close_location"] >= 0.58)
        )
        sell = (
            (high >= frame["bb_upper"])
            & (close < frame["bb_upper"])
            & (close < open_)
            & (hist < prev_hist)
            & (frame["rsi14"] >= rsi_high - 12.0)
            & (frame["close_location"] <= 0.42)
        )
    elif family == "bb_macd_breakout":
        was_narrow = prev["bb_width"] <= prev["bb_width_median"] * 0.85
        buy = (
            was_narrow
            & (prev["close"] <= prev["bb_upper"])
            & (close > frame["bb_upper"])
            & (hist > 0.0)
            & (hist > prev_hist)
            & (frame["volume_ratio"] >= 0.9)
            & (frame["close_location"] >= 0.7)
        )
        sell = (
            was_narrow
            & (prev["close"] >= prev["bb_lower"])
            & (close < frame["bb_lower"])
            & (hist < 0.0)
            & (hist < prev_hist)
            & (frame["volume_ratio"] >= 0.9)
            & (frame["close_location"] <= 0.3)
        )
    elif family == "bb_macd_trend_pullback":
        buy = (
            trend_up
            & (frame["adx14"] >= adx_min * 0.8)
            & (low <= frame["bb_mid"])
            & (close > frame["bb_mid"])
            & (close > open_)
            & (hist > prev_hist)
            & (frame["rsi14"].between(45.0, 68.0))
        )
        sell = (
            trend_down
            & (frame["adx14"] >= adx_min * 0.8)
            & (high >= frame["bb_mid"])
            & (close < frame["bb_mid"])
            & (close < open_)
            & (hist < prev_hist)
            & (frame["rsi14"].between(32.0, 55.0))
        )
    elif family == "macd_adx_continuation":
        buy = trend_up & (frame["adx14"] >= adx_min) & (prev_hist <= 0.0) & (hist > 0.0) & (close > frame["ema20"])
        sell = trend_down & (frame["adx14"] >= adx_min) & (prev_hist >= 0.0) & (hist < 0.0) & (close < frame["ema20"])
    elif family == "rsi_macd_reversal":
        buy = (
            (prev["rsi14"] <= rsi_low)
            & (frame["rsi14"] > prev["rsi14"])
            & (hist > prev_hist)
            & (frame["bb_percent_b"] <= 0.35)
            & (close > open_)
        )
        sell = (
            (prev["rsi14"] >= rsi_high)
            & (frame["rsi14"] < prev["rsi14"])
            & (hist < prev_hist)
            & (frame["bb_percent_b"] >= 0.65)
            & (close < open_)
        )
    elif family == "stoch_bb_reversion":
        buy = (
            (prev["stoch_k"] <= 25.0)
            & (prev["stoch_k"] <= prev["stoch_d"])
            & (frame["stoch_k"] > frame["stoch_d"])
            & (low <= frame["bb_lower"] + frame["atr14"] * 0.15)
            & (close > open_)
        )
        sell = (
            (prev["stoch_k"] >= 75.0)
            & (prev["stoch_k"] >= prev["stoch_d"])
            & (frame["stoch_k"] < frame["stoch_d"])
            & (high >= frame["bb_upper"] - frame["atr14"] * 0.15)
            & (close < open_)
        )
    elif family == "cci_macd_momentum":
        buy = trend_up & (prev["cci20"] <= 100.0) & (frame["cci20"] > 100.0) & (hist > 0.0) & (hist > prev_hist)
        sell = trend_down & (prev["cci20"] >= -100.0) & (frame["cci20"] < -100.0) & (hist < 0.0) & (hist < prev_hist)
    elif family == "bb_keltner_squeeze":
        buy = (
            prev["squeeze"].fillna(False)
            & (~frame["squeeze"].fillna(False))
            & (close > frame["bb_upper"])
            & (hist > 0.0)
            & (hist > prev_hist)
            & (frame["volume_ratio"] >= 0.9)
        )
        sell = (
            prev["squeeze"].fillna(False)
            & (~frame["squeeze"].fillna(False))
            & (close < frame["bb_lower"])
            & (hist < 0.0)
            & (hist < prev_hist)
            & (frame["volume_ratio"] >= 0.9)
        )
    elif family == "vwap_macd_reclaim":
        buy = trend_up & (prev["close"] <= prev["vwap"]) & (close > frame["vwap"]) & (hist > prev_hist) & (close > open_)
        sell = trend_down & (prev["close"] >= prev["vwap"]) & (close < frame["vwap"]) & (hist < prev_hist) & (close < open_)
    elif family == "supertrend_macd_pullback":
        buy = (
            (frame["supertrend_dir"] > 0)
            & trend_up
            & (low <= frame["ema20"] + frame["atr14"] * 0.12)
            & (close > frame["ema20"])
            & (hist > prev_hist)
            & (close > open_)
        )
        sell = (
            (frame["supertrend_dir"] < 0)
            & trend_down
            & (high >= frame["ema20"] - frame["atr14"] * 0.12)
            & (close < frame["ema20"])
            & (hist < prev_hist)
            & (close < open_)
        )
    else:
        raise ValueError(f"Unknown family: {family}")

    buy_array = buy.fillna(False).to_numpy(dtype=bool, copy=True)
    sell_array = sell.fillna(False).to_numpy(dtype=bool, copy=True)
    conflict = buy_array & sell_array
    buy_array[conflict] = False
    sell_array[conflict] = False
    return buy_array, sell_array


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnl_values = [float(row["pnl"]) for row in trades]
    wins = [value for value in pnl_values if value > 0.005]
    losses = [value for value in pnl_values if value < -0.005]
    flats = len(pnl_values) - len(wins) - len(losses)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    balance = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for row in sorted(trades, key=lambda item: item["exit_time"]):
        value = float(row["pnl"])
        balance += value
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
        streak = streak + 1 if value < -0.005 else 0
        max_streak = max(max_streak, streak)
    decided = len(wins) + len(losses)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flat": flats,
        "win_rate_pct": round(100.0 * len(wins) / max(1, decided), 2),
        "pnl_001": round(sum(pnl_values), 2),
        "final_balance_from_1000": round(1000.0 + sum(pnl_values), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0.0 else None,
        "max_closed_drawdown": round(max_dd, 2),
        "profit_to_drawdown": round(sum(pnl_values) / abs(max_dd), 3) if max_dd < 0.0 else None,
        "average_trade": round(sum(pnl_values) / max(1, len(pnl_values)), 3),
        "average_win": round(gross_win / max(1, len(wins)), 3),
        "average_loss": round(sum(losses) / max(1, len(losses)), 3),
        "max_consecutive_losses": max_streak,
        "timeouts": sum(row["status"] == "timeout" for row in trades),
    }


def _simulate(
    signal_frame: pd.DataFrame,
    buy: np.ndarray,
    sell: np.ndarray,
    timeframe_minutes: int,
    exit_cfg: dict[str, float | int],
    m1: pd.DataFrame,
    start: datetime,
    end: datetime,
    point: float,
    profit_per_usd_001: float,
    commission_per_001: float,
    keep_trades: bool = False,
    sl_distance_multiplier: float = 1.0,
    tp_distance_multiplier: float = 1.0,
    be_trigger_multiplier: float = 1.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    m1_times = m1["time"].to_numpy(dtype="datetime64[ns]")
    m1_open = m1["open"].to_numpy(dtype=float)
    m1_high = m1["high"].to_numpy(dtype=float)
    m1_low = m1["low"].to_numpy(dtype=float)
    m1_close = m1["close"].to_numpy(dtype=float)
    m1_spread = m1["spread"].to_numpy(dtype=float) * point
    signal_times = signal_frame["time"].to_numpy(dtype="datetime64[ns]")
    atr_values = signal_frame["atr14"].to_numpy(dtype=float)
    signal_indices = np.flatnonzero(buy | sell)
    start64 = np.datetime64(pd.Timestamp(start).to_datetime64())
    end64 = np.datetime64(pd.Timestamp(end).to_datetime64())
    next_allowed = start64
    trades: list[dict[str, Any]] = []

    for signal_index in signal_indices:
        entry_time = signal_times[signal_index] + np.timedelta64(int(timeframe_minutes), "m")
        if entry_time < start64 or entry_time >= end64 or entry_time < next_allowed:
            continue
        entry_idx = int(np.searchsorted(m1_times, entry_time, side="left"))
        if entry_idx >= len(m1) or m1_times[entry_idx] >= end64:
            continue
        side = "buy" if buy[signal_index] else "sell"
        spread = float(m1_spread[entry_idx])
        bid = float(m1_open[entry_idx])
        entry = bid + spread if side == "buy" else bid
        atr_value = float(atr_values[signal_index])
        if not math.isfinite(atr_value) or atr_value <= 0.0:
            continue
        base_stop_distance = min(12.0, max(1.50, atr_value * float(exit_cfg["sl_atr"])))
        stop_distance = base_stop_distance * max(0.1, float(sl_distance_multiplier))
        target_distance = max(0.25, base_stop_distance * float(exit_cfg["tp_r"]) * max(0.1, float(tp_distance_multiplier)))
        initial_sl = entry - stop_distance if side == "buy" else entry + stop_distance
        target = entry + target_distance if side == "buy" else entry - target_distance
        current_sl = initial_sl
        horizon = m1_times[entry_idx] + np.timedelta64(int(exit_cfg["hold_minutes"]), "m")
        end_idx = min(int(np.searchsorted(m1_times, min(horizon, end64), side="right")), len(m1))
        if end_idx <= entry_idx:
            continue
        status = "timeout"
        exit_idx = end_idx - 1
        exit_price = float(m1_close[exit_idx]) if side == "buy" else float(m1_close[exit_idx] + m1_spread[exit_idx])
        be_trigger = base_stop_distance * float(exit_cfg["be_r"]) * max(0.1, float(be_trigger_multiplier))
        for index in range(entry_idx, end_idx):
            high = float(m1_high[index])
            low = float(m1_low[index])
            ask_high = high + float(m1_spread[index])
            ask_low = low + float(m1_spread[index])
            hit_sl = low <= current_sl if side == "buy" else ask_high >= current_sl
            hit_tp = high >= target if side == "buy" else ask_low <= target
            if hit_sl:
                status, exit_idx, exit_price = "stop", index, current_sl
                break
            if hit_tp:
                status, exit_idx, exit_price = "target", index, target
                break
            favorable = high - entry if side == "buy" else entry - ask_low
            if be_trigger > 0.0 and favorable >= be_trigger:
                buffer_value = float(exit_cfg["be_buffer"])
                candidate = entry + buffer_value if side == "buy" else entry - buffer_value
                current_sl = max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)
        move = exit_price - entry if side == "buy" else entry - exit_price
        pnl = move * profit_per_usd_001 - commission_per_001
        trades.append(
            {
                "signal_time": pd.Timestamp(signal_times[signal_index]).isoformat(),
                "entry_time": pd.Timestamp(m1_times[entry_idx]).isoformat(),
                "exit_time": pd.Timestamp(m1_times[exit_idx]).isoformat(),
                "side": side,
                "entry": round(entry, 3),
                "initial_sl": round(initial_sl, 3),
                "final_sl": round(current_sl, 3),
                "tp": round(target, 3),
                "status": status,
                "pnl": round(pnl, 4),
            }
        )
        next_allowed = m1_times[exit_idx] + np.timedelta64(int(exit_cfg["cooldown"]), "m")
    return _summary(trades), trades if keep_trades else []


def _simulate_three_leg(
    signal_frame: pd.DataFrame,
    buy: np.ndarray,
    sell: np.ndarray,
    timeframe_minutes: int,
    m1: pd.DataFrame,
    start: datetime,
    end: datetime,
    point: float,
    profit_per_usd_001: float,
    commission_per_001: float,
) -> dict[str, Any]:
    m1_times = m1["time"].to_numpy(dtype="datetime64[ns]")
    m1_open = m1["open"].to_numpy(dtype=float)
    m1_high = m1["high"].to_numpy(dtype=float)
    m1_low = m1["low"].to_numpy(dtype=float)
    m1_close = m1["close"].to_numpy(dtype=float)
    m1_spread = m1["spread"].to_numpy(dtype=float) * point
    signal_times = signal_frame["time"].to_numpy(dtype="datetime64[ns]")
    atr_values = signal_frame["atr14"].to_numpy(dtype=float)
    signal_indices = np.flatnonzero(buy | sell)
    start64 = np.datetime64(pd.Timestamp(start).to_datetime64())
    end64 = np.datetime64(pd.Timestamp(end).to_datetime64())
    next_allowed = start64
    trades: list[dict[str, Any]] = []
    batch_pnl: dict[int, float] = defaultdict(float)
    batch = 0
    leg_plan = (
        ("TP09", 0.90, 0.0),
        ("TP125", 1.25, 0.75),
        ("TP18", 1.80, 1.00),
    )

    for signal_index in signal_indices:
        entry_time = signal_times[signal_index] + np.timedelta64(int(timeframe_minutes), "m")
        if entry_time < start64 or entry_time >= end64 or entry_time < next_allowed:
            continue
        entry_idx = int(np.searchsorted(m1_times, entry_time, side="left"))
        if entry_idx >= len(m1) or m1_times[entry_idx] >= end64:
            continue
        side = "buy" if buy[signal_index] else "sell"
        spread = float(m1_spread[entry_idx])
        bid = float(m1_open[entry_idx])
        entry = bid + spread if side == "buy" else bid
        atr_value = float(atr_values[signal_index])
        if not math.isfinite(atr_value) or atr_value <= 0.0:
            continue
        stop_distance = min(12.0, max(1.50, atr_value * 1.6))
        initial_sl = entry - stop_distance if side == "buy" else entry + stop_distance
        batch += 1
        latest_exit_idx = entry_idx
        for leg_name, target_r, be_r in leg_plan:
            target_distance = max(1.0, stop_distance * target_r)
            target = entry + target_distance if side == "buy" else entry - target_distance
            current_sl = initial_sl
            horizon = m1_times[entry_idx] + np.timedelta64(360, "m")
            end_idx = min(int(np.searchsorted(m1_times, min(horizon, end64), side="right")), len(m1))
            if end_idx <= entry_idx:
                continue
            status = "timeout"
            exit_idx = end_idx - 1
            exit_price = float(m1_close[exit_idx]) if side == "buy" else float(m1_close[exit_idx] + m1_spread[exit_idx])
            for index in range(entry_idx, end_idx):
                high = float(m1_high[index])
                low = float(m1_low[index])
                ask_high = high + float(m1_spread[index])
                ask_low = low + float(m1_spread[index])
                hit_sl = low <= current_sl if side == "buy" else ask_high >= current_sl
                hit_tp = high >= target if side == "buy" else ask_low <= target
                if hit_sl:
                    status, exit_idx, exit_price = "stop", index, current_sl
                    break
                if hit_tp:
                    status, exit_idx, exit_price = "target", index, target
                    break
                favorable = high - entry if side == "buy" else entry - ask_low
                if be_r > 0.0 and favorable >= stop_distance * be_r:
                    candidate = entry + 0.10 if side == "buy" else entry - 0.10
                    current_sl = max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)
            move = exit_price - entry if side == "buy" else entry - exit_price
            pnl = move * profit_per_usd_001 - commission_per_001
            batch_pnl[batch] += pnl
            latest_exit_idx = max(latest_exit_idx, exit_idx)
            trades.append(
                {
                    "batch": batch,
                    "leg": leg_name,
                    "entry_time": pd.Timestamp(m1_times[entry_idx]).isoformat(),
                    "exit_time": pd.Timestamp(m1_times[exit_idx]).isoformat(),
                    "side": side,
                    "status": status,
                    "pnl": round(pnl, 4),
                }
            )
        next_allowed = m1_times[latest_exit_idx] + np.timedelta64(15, "m")

    leg_summary = _summary(trades)
    batch_values = list(batch_pnl.values())
    batch_wins = sum(value > 0.005 for value in batch_values)
    batch_losses = sum(value < -0.005 for value in batch_values)
    leg_summary.update(
        {
            "setups": len(batch_values),
            "positive_setups": batch_wins,
            "negative_setups": batch_losses,
            "setup_win_rate_pct": round(100.0 * batch_wins / max(1, batch_wins + batch_losses), 2),
            "lot_per_setup": 0.03,
            "leg_plan": ["0.01 lot TP0.9R", "0.01 lot TP1.25R BE0.75R", "0.01 lot TP1.8R BE1R"],
        }
    )
    return leg_summary


def _score(row: dict[str, Any]) -> float:
    train = row["train_40_sessions"]
    test = row["test_20_sessions"]
    full = row["full_60_sessions"]
    train_pf = min(5.0, float(train["profit_factor"] or 0.0))
    test_pf = min(5.0, float(test["profit_factor"] or 0.0))
    pf_floor = min(train_pf, test_pf)
    wr_floor = min(float(train["win_rate_pct"]), float(test["win_rate_pct"]))
    pdd = max(-3.0, min(5.0, float(full["profit_to_drawdown"] or -3.0)))
    sample = min(3.0, math.log10(max(1, int(full["trades"]))))
    return round(pf_floor * 30.0 + wr_floor * 0.35 + pdd * 12.0 + sample * 3.0, 4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--commission-per-001", type=float, default=0.06)
    parser.add_argument(
        "--output",
        default="data_vantage/bollinger_macd_hybrid_grid_60sessions_20260810.json",
    )
    args = parser.parse_args()

    load_dotenv(Path(args.env).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC)
        fetch_start = end - timedelta(days=max(125, int(args.sessions * 2.0)))
        warmup = timedelta(days=5)
        m1 = _rates(symbol, "M1", fetch_start - warmup, end + timedelta(hours=1))
        m5 = _rates(symbol, "M5", fetch_start - warmup, end + timedelta(hours=1))
        session_key = (m1["time"] + pd.Timedelta(hours=2)).dt.date
        counts = m1.loc[m1["time"] < pd.Timestamp(end)].groupby(session_key).size()
        completed = [day for day, count in counts.items() if int(count) >= 180]
        if len(completed) < int(args.sessions):
            raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {args.sessions}")
        selected = completed[-int(args.sessions):]
        train_sessions = selected[:40]
        test_sessions = selected[40:]
        selected_mask = session_key.isin(selected)
        train_mask = session_key.isin(train_sessions)
        test_mask = session_key.isin(test_sessions)
        full_start = m1.loc[selected_mask, "time"].iloc[0].to_pydatetime()
        train_start = m1.loc[train_mask, "time"].iloc[0].to_pydatetime()
        test_start = m1.loc[test_mask, "time"].iloc[0].to_pydatetime()
        full_end = end
        train_end = test_start
        test_end = end
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        anchor = float(m1.iloc[-1]["close"])
        profit_per_usd_001 = abs(
            float(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, anchor, anchor + 1.0) or 0.0)
        )
        if profit_per_usd_001 <= 0.0:
            raise RuntimeError("Could not calculate 0.01-lot XAUUSD value")

        frames = {"M1": m1, "M5": m5}
        prepared: dict[tuple[str, str], pd.DataFrame] = {}
        signal_cache: dict[tuple[str, str, str], tuple[np.ndarray, np.ndarray]] = {}
        for timeframe, raw_frame in frames.items():
            for preset_name, preset in PRESETS.items():
                enriched = _prepare(raw_frame, preset)
                prepared[(timeframe, preset_name)] = enriched
                for family in FAMILIES:
                    signal_cache[(timeframe, preset_name, family)] = _signals(enriched, family, preset)

        rows: list[dict[str, Any]] = []
        definitions: dict[str, dict[str, Any]] = {}
        for family in FAMILIES:
            for timeframe, timeframe_minutes in (("M1", 1), ("M5", 5)):
                for preset_name, preset in PRESETS.items():
                    frame = prepared[(timeframe, preset_name)]
                    buy, sell = signal_cache[(timeframe, preset_name, family)]
                    for exit_name, exit_cfg in EXITS.items():
                        name = f"{family}:{timeframe}:{preset_name}:{exit_name}"
                        definitions[name] = {
                            "family": family,
                            "timeframe": timeframe,
                            "timeframe_minutes": timeframe_minutes,
                            "preset": preset_name,
                            "exit": exit_name,
                        }
                        train, _ = _simulate(
                            frame,
                            buy,
                            sell,
                            timeframe_minutes,
                            exit_cfg,
                            m1,
                            train_start,
                            train_end,
                            point,
                            profit_per_usd_001,
                            float(args.commission_per_001),
                        )
                        test, _ = _simulate(
                            frame,
                            buy,
                            sell,
                            timeframe_minutes,
                            exit_cfg,
                            m1,
                            test_start,
                            test_end,
                            point,
                            profit_per_usd_001,
                            float(args.commission_per_001),
                        )
                        full, _ = _simulate(
                            frame,
                            buy,
                            sell,
                            timeframe_minutes,
                            exit_cfg,
                            m1,
                            full_start,
                            full_end,
                            point,
                            profit_per_usd_001,
                            float(args.commission_per_001),
                        )
                        row = {
                            "name": name,
                            "family": family,
                            "timeframe": timeframe,
                            "preset": preset_name,
                            "exit": exit_name,
                            "train_40_sessions": train,
                            "test_20_sessions": test,
                            "full_60_sessions": full,
                        }
                        row["robust"] = bool(
                            int(train["trades"]) >= 12
                            and int(test["trades"]) >= 6
                            and int(full["trades"]) >= 25
                            and float(train["pnl_001"]) > 0.0
                            and float(test["pnl_001"]) > 0.0
                            and float(train["profit_factor"] or 0.0) > 1.0
                            and float(test["profit_factor"] or 0.0) > 1.0
                        )
                        row["score"] = _score(row)
                        rows.append(row)

        ranked = sorted(rows, key=lambda item: (bool(item["robust"]), float(item["score"])), reverse=True)
        best_by_family: list[dict[str, Any]] = []
        for family in FAMILIES:
            family_rows = [row for row in ranked if row["family"] == family]
            best_by_family.append(family_rows[0])
        best_by_family.sort(key=lambda item: (bool(item["robust"]), float(item["score"])), reverse=True)

        three_leg_rows: list[dict[str, Any]] = []
        for row in best_by_family:
            definition = definitions[row["name"]]
            frame = prepared[(definition["timeframe"], definition["preset"])]
            buy, sell = signal_cache[(definition["timeframe"], definition["preset"], definition["family"])]
            three_train = _simulate_three_leg(
                frame,
                buy,
                sell,
                int(definition["timeframe_minutes"]),
                m1,
                train_start,
                train_end,
                point,
                profit_per_usd_001,
                float(args.commission_per_001),
            )
            three_test = _simulate_three_leg(
                frame,
                buy,
                sell,
                int(definition["timeframe_minutes"]),
                m1,
                test_start,
                test_end,
                point,
                profit_per_usd_001,
                float(args.commission_per_001),
            )
            three_full = _simulate_three_leg(
                frame,
                buy,
                sell,
                int(definition["timeframe_minutes"]),
                m1,
                full_start,
                full_end,
                point,
                profit_per_usd_001,
                float(args.commission_per_001),
            )
            three_leg_rows.append(
                {
                    "source_candidate": row["name"],
                    "family": row["family"],
                    "train_40_sessions": three_train,
                    "test_20_sessions": three_test,
                    "full_60_sessions": three_full,
                    "robust": bool(
                        int(three_train["setups"]) >= 10
                        and int(three_test["setups"]) >= 5
                        and float(three_train["pnl_001"]) > 0.0
                        and float(three_test["pnl_001"]) > 0.0
                    ),
                }
            )
        three_leg_rows.sort(
            key=lambda item: (
                bool(item["robust"]),
                min(float(item["train_40_sessions"]["profit_factor"] or 0.0), float(item["test_20_sessions"]["profit_factor"] or 0.0)),
                float(item["full_60_sessions"]["pnl_001"]),
            ),
            reverse=True,
        )

        detailed: dict[str, list[dict[str, Any]]] = {}
        for row in ranked[:10]:
            definition = definitions[row["name"]]
            frame = prepared[(definition["timeframe"], definition["preset"])]
            buy, sell = signal_cache[(definition["timeframe"], definition["preset"], definition["family"])]
            _metrics, trades = _simulate(
                frame,
                buy,
                sell,
                int(definition["timeframe_minutes"]),
                EXITS[str(definition["exit"])],
                m1,
                full_start,
                full_end,
                point,
                profit_per_usd_001,
                float(args.commission_per_001),
                keep_trades=True,
            )
            detailed[row["name"]] = trades

        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {"start": full_start.isoformat(), "split": test_start.isoformat(), "end": full_end.isoformat()},
            "sessions": [day.isoformat() for day in selected],
            "symbol": symbol,
            "start_balance": 1000.0,
            "lot_per_trade": 0.01,
            "candidate_count": len(rows),
            "robust_candidate_count": sum(bool(row["robust"]) for row in rows),
            "method": (
                "Closed M1/M5 signal candle, market entry on next M1 bar, one position per candidate, dynamic M1 spread, "
                "0.06 USD commission per 0.01 lot, conservative SL-first same-bar ordering, 40-session train plus "
                "20-session holdout and fixed 0.01 lot without compounding"
            ),
            "families": FAMILIES,
            "presets": PRESETS,
            "exit_profiles": EXITS,
            "top20": ranked[:20],
            "best_by_family": best_by_family,
            "three_leg_validation": three_leg_rows,
            "all_results": rows,
            "top10_trades": detailed,
            "sources": [
                "https://www.mql5.com/en/docs/indicators/ibands",
                "https://www.mql5.com/en/docs/indicators/imacd",
                "https://www.mql5.com/en/docs/indicators",
            ],
            "limitations": [
                "The same 60 sessions were used to select parameters; the final 20-session holdout reduces but does not eliminate overfitting.",
                "Fixed 0.01 lot isolates signal quality. Three-leg and dynamic-lot execution require a separate portfolio validation.",
                "Closed-balance drawdown is reported; tick-level slippage and floating equity drawdown are not reconstructed.",
                "No candidate is enabled in the live bot by this research script.",
            ],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        brief = {
            "output": str(out),
            "candidate_count": output["candidate_count"],
            "robust_candidate_count": output["robust_candidate_count"],
            "best_by_family": [
                {
                    "name": row["name"],
                    "robust": row["robust"],
                    "score": row["score"],
                    "full": row["full_60_sessions"],
                    "test": row["test_20_sessions"],
                }
                for row in best_by_family
            ],
            "three_leg_validation": three_leg_rows,
        }
        print(json.dumps(brief, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
