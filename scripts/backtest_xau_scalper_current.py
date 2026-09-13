from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.scalp_setups import EXTRA_SETUP_MODES, INDICATOR_SETUP_PROFILES, evaluate_extra_setup
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from scripts.history_cache import load_cached_rates


@dataclass
class Leg:
    side: str
    entry: float
    sl: float
    tp: float
    leg: str
    opened_idx: int
    setup_tag: str = ""
    lot: float = 0.0
    spread_price: float = 0.0
    be_trigger: float = 0.0
    be_buffer: float = 0.0
    be_enabled: bool = True
    max_hold_until_idx: int = -1
    min_hold_until_idx: int = -1
    status: str = "open"
    exit: float = 0.0
    closed_idx: int = -1
    initial_sl: float = 0.0

    def __post_init__(self) -> None:
        if self.initial_sl == 0.0:
            self.initial_sl = self.sl


def _tf(name: str) -> int:
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}[name.upper()]


def _rates(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    history_dir = str(os.getenv("BACKTEST_HISTORY_DIR", "") or "").strip()
    if history_dir:
        return enrich(load_cached_rates(history_dir, timeframe, start, end))
    raw = mt5.copy_rates_range(symbol, _tf(timeframe), start, end)
    if raw is None or len(raw) == 0:
        # MT5 can reject long M1 date ranges close to the terminal's
        # MaxBars limit even though those bars are available by position.
        raw = mt5.copy_rates_from_pos(symbol, _tf(timeframe), 0, 99_999)
        if raw is None or len(raw) == 0:
            return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    frame = frame[(frame["time"] >= start_ts) & (frame["time"] <= end_ts)]
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "true" if default else "false") or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    info = mt5.symbol_info(symbol)
    contract_size = max(0.0, float(getattr(info, "trade_contract_size", 0.0) or 0.0))
    if contract_size <= 0.0:
        raise RuntimeError(f"Missing contract size for {symbol}")
    move = exit_price - entry if side == "buy" else entry - exit_price
    return float(move * float(lot) * contract_size)


def _hit_tp(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _hit_sl(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _better_stop(side: str, current_sl: float, candidate: float) -> float:
    return max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)


def _row_at(frame: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    idx = _search_time(frame, ts, side="right") - 1
    if idx < 0 or idx >= len(frame):
        return None
    return frame.iloc[idx]


def _ts(value: Any) -> pd.Timestamp:
    return pd.to_datetime(value, utc=True)


def _search_time(frame: pd.DataFrame, value: Any, *, side: str = "left") -> int:
    target = _ts(value)
    return int(frame["time"].searchsorted(target, side=side))


def _align_frames(m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame) -> pd.DataFrame:
    m1 = m1.copy()
    m5p = m5.copy()
    # MT5 timestamps bars at their open. Higher-timeframe values become
    # tradable information only after that bar closes.
    m5p["time"] = m5p["time"] + pd.Timedelta(minutes=5)
    m5_columns = [column for column in m5p.columns if column != "time"]
    for column in m5_columns:
        m5p[f"prev_{column}"] = m5p[column].shift(1)
    m5p = m5p.rename(columns={column: f"m5_{column}" for column in m5p.columns if column != "time"})
    m15p = m15[["time", "ema20", "ema50"]].copy()
    m15p["time"] = m15p["time"] + pd.Timedelta(minutes=15)
    m15p = m15p.rename(columns={"ema20": "m15_ema20", "ema50": "m15_ema50"})
    for frame in (m1, m5p, m15p):
        frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.as_unit("ns")
    out = pd.merge_asof(m1.sort_values("time"), m5p.sort_values("time"), on="time", direction="backward")
    out = pd.merge_asof(out.sort_values("time"), m15p.sort_values("time"), on="time", direction="backward")
    return out


def _signal_from_rows(m1: pd.DataFrame, idx: int, cfg: Any) -> tuple[dict | None, dict]:
    last1 = m1.iloc[idx]
    prev1 = m1.iloc[idx - 1]
    prev2 = m1.iloc[idx - 2]
    prev3 = m1.iloc[idx - 3]
    if pd.isna(last1.get("m5_close")) or pd.isna(last1.get("m15_ema20")):
        return None, {"reason": "higher_tf_not_ready"}
    if pd.isna(last1.get("m5_prev_close")):
        return None, {"reason": "m5_prev_not_ready"}
    atr1 = float(last1.get("atr14", 0.0) or 0.0)
    atr5 = float(last1.get("m5_atr14", atr1) or atr1) if "m5_atr14" in last1 else atr1
    if atr1 <= 0 or atr5 <= 0:
        return None, {"reason": "atr_not_ready"}

    min_m5_adx = _env_float("XAU_SCALP_MIN_M5_ADX", 14.0)
    trend_buy = (
        float(last1["m15_ema20"]) > float(last1["m15_ema50"])
        and float(last1["m5_ema20"]) > float(last1["m5_ema50"])
        and float(last1["m5_close"]) > float(last1["m5_ema20"])
        and float(last1["m5_adx14"]) >= min_m5_adx
    )
    trend_sell = (
        float(last1["m15_ema20"]) < float(last1["m15_ema50"])
        and float(last1["m5_ema20"]) < float(last1["m5_ema50"])
        and float(last1["m5_close"]) < float(last1["m5_ema20"])
        and float(last1["m5_adx14"]) >= min_m5_adx
    )

    close1 = float(last1["close"])
    ema1 = float(last1["ema20"])
    prev_close = float(prev1["close"])
    prev_ema = float(prev1["ema20"])
    body = abs(float(last1["close"]) - float(last1["open"]))
    recent_m1 = m1.iloc[max(0, idx - 3) : idx + 1]
    recent_move = close1 - float(recent_m1.iloc[0]["open"])
    m5_body = float(last1["m5_close"]) - float(last1["m5_open"])
    m5_progress = float(last1["m5_close"]) - float(last1["m5_prev_close"])
    max_chase = max(1.2, float(cfg.xau_scalp_tp1_usd) * 0.75)
    near_ema = abs(close1 - ema1) <= max_chase

    opposite_enabled = _env_bool("XAU_SCALP_OPPOSITE_IMPULSE_FILTER_ENABLED", True)
    opposite_buy = opposite_enabled and (recent_move < -(atr1 * 1.0) or m5_body < -(atr1 * 0.65) or m5_progress < -(atr1 * 0.75))
    opposite_sell = opposite_enabled and (recent_move > (atr1 * 1.0) or m5_body > (atr1 * 0.65) or m5_progress > (atr1 * 0.75))
    chase_enabled = _env_bool("XAU_SCALP_SAME_DIRECTION_CHASE_FILTER_ENABLED", True)
    max_m1_chase = max(0.5, _env_float("XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD", 2.4))
    max_m5_chase = max(0.5, _env_float("XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD", 3.5))
    buy_chase = chase_enabled and (recent_move > max_m1_chase or m5_progress > max_m5_chase)
    sell_chase = chase_enabled and (recent_move < -max_m1_chase or m5_progress < -max_m5_chase)

    trigger_mode = str(os.getenv("XAU_SCALP_TRIGGER_MODE", "trend_pullback") or "trend_pullback").strip().lower()
    extra_diagnostics: dict[str, Any] = {}
    buy = trend_buy and near_ema and not opposite_buy and not buy_chase and prev_close <= prev_ema + 0.25 and close1 > ema1 and float(last1["rsi14"]) >= 52.0 and close1 > float(last1["open"]) and body >= max(0.12, atr1 * 0.15)
    sell = trend_sell and near_ema and not opposite_sell and not sell_chase and prev_close >= prev_ema - 0.25 and close1 < ema1 and float(last1["rsi14"]) <= 48.0 and close1 < float(last1["open"]) and body >= max(0.12, atr1 * 0.15)
    setup_tag = trigger_mode
    if trigger_mode == "range_reversion":
        max_adx = _env_float("XAU_SCALP_RANGE_MAX_M5_ADX", 18.0)
        max_ema_separation = _env_float("XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR", 0.55)
        rsi_edge = _env_float("XAU_SCALP_RANGE_RSI_EDGE", 35.0)
        ema_separation = abs(float(last1["m5_ema20"]) - float(last1["m5_ema50"])) / max(atr5, 0.00001)
        range_regime = float(last1["m5_adx14"]) <= max_adx and ema_separation <= max_ema_separation
        lower_wick = min(float(last1["open"]), close1) - float(last1["low"])
        upper_wick = float(last1["high"]) - max(float(last1["open"]), close1)
        buy = (
            range_regime
            and float(last1["low"]) <= float(last1["bb_lower"])
            and close1 > float(last1["bb_lower"])
            and float(last1["rsi14"]) <= rsi_edge
            and close1 > float(last1["open"])
            and lower_wick >= body * 0.35
        )
        sell = (
            range_regime
            and float(last1["high"]) >= float(last1["bb_upper"])
            and close1 < float(last1["bb_upper"])
            and float(last1["rsi14"]) >= (100.0 - rsi_edge)
            and close1 < float(last1["open"])
            and upper_wick >= body * 0.35
        )
    elif trigger_mode == "range_breakout":
        min_adx = _env_float("XAU_SCALP_BREAKOUT_MIN_M5_ADX", 16.0)
        min_body_atr = _env_float("XAU_SCALP_BREAKOUT_MIN_BODY_ATR", 0.55)
        min_volume_ratio = _env_float("XAU_SCALP_BREAKOUT_MIN_VOLUME_RATIO", 1.05)
        volume_ratio = float(last1["tick_volume"]) / max(float(last1["volume_ma20"]), 1.0)
        buy = (
            float(last1["m5_adx14"]) >= min_adx
            and float(last1["m15_ema20"]) > float(last1["m15_ema50"])
            and close1 > float(last1["hh20"])
            and close1 > float(last1["open"])
            and body >= atr1 * min_body_atr
            and volume_ratio >= min_volume_ratio
            and not buy_chase
        )
        sell = (
            float(last1["m5_adx14"]) >= min_adx
            and float(last1["m15_ema20"]) < float(last1["m15_ema50"])
            and close1 < float(last1["ll20"])
            and close1 < float(last1["open"])
            and body >= atr1 * min_body_atr
            and volume_ratio >= min_volume_ratio
            and not sell_chase
        )
    elif trigger_mode in EXTRA_SETUP_MODES:
        # The aligned frame stores the latest two closed M5 bars on the M1 row.
        proxy5 = last1.copy()
        previous_proxy5 = last1.copy()
        proxy15 = last1.copy()
        m5_fields = (
            "open", "high", "low", "close", "tick_volume", "ema20", "ema50", "ema200", "rsi14", "atr14",
            "adx14", "bb_mid", "bb_upper", "bb_lower", "bb_width", "bb_width_median", "bb_percent_b",
            "macd", "macd_signal", "macd_hist", "bb_fast_mid", "bb_fast_upper", "bb_fast_lower",
            "macd_fast", "macd_fast_signal", "macd_fast_hist", "bb_slow_mid", "bb_slow_upper",
            "bb_slow_lower", "bb_slow_percent_b", "macd_slow", "macd_slow_signal", "macd_slow_hist",
            "keltner_fast_upper", "keltner_fast_lower", "bb_keltner_fast_squeeze", "stoch_k", "stoch_d",
            "close_location", "volume_ma20", "volume_ratio", "vwap",
        )
        for key in m5_fields:
            m5_key = f"m5_{key}"
            if m5_key in last1:
                proxy5[key] = last1[m5_key]
            previous_key = f"m5_prev_{key}"
            if previous_key in last1:
                previous_proxy5[key] = last1[previous_key]
        proxy15["ema20"] = last1["m15_ema20"]
        proxy15["ema50"] = last1["m15_ema50"]
        buy, sell, extra_diagnostics = evaluate_extra_setup(
            trigger_mode, last1, prev1, proxy5, previous_proxy5, proxy15, atr1, atr5, recent_move, prev2, prev3
        )
        setup_tag = str(extra_diagnostics.get("setup_tag", "") or extra_diagnostics.get("selected_setup", "") or trigger_mode)
    diagnostics = {
        "trigger_mode": trigger_mode,
        "trend_buy": trend_buy,
        "trend_sell": trend_sell,
        "near_ema": near_ema,
        "opposite_buy": opposite_buy,
        "opposite_sell": opposite_sell,
        "buy_chase": buy_chase,
        "sell_chase": sell_chase,
    }
    if _env_bool("XAU_SCALP_QUALITY_FILTER_ENABLED", False):
        candle_range = max(0.00001, float(last1["high"]) - float(last1["low"]))
        close_location = (close1 - float(last1["low"])) / candle_range
        ema_separation_atr = abs(float(last1["m5_ema20"]) - float(last1["m5_ema50"])) / max(atr5, 0.00001)
        ema20_slope_atr = abs(float(last1["m5_ema20"]) - float(last1["m5_prev_ema20"])) / max(atr5, 0.00001)
        min_close_location = _env_float("XAU_SCALP_QUALITY_MIN_CLOSE_LOCATION", 0.65)
        structure_ok = (
            ema_separation_atr >= _env_float("XAU_SCALP_QUALITY_MIN_EMA_SEPARATION_ATR", 0.15)
            and ema20_slope_atr >= _env_float("XAU_SCALP_QUALITY_MIN_EMA_SLOPE_ATR", 0.03)
        )
        buy = buy and structure_ok and close_location >= min_close_location
        sell = sell and structure_ok and close_location <= (1.0 - min_close_location)
        diagnostics.update(
            {
                "quality_filter": True,
                "close_location": close_location,
                "ema_separation_atr": ema_separation_atr,
                "ema20_slope_atr": ema20_slope_atr,
            }
        )
    if not buy and not sell:
        return None, diagnostics
    side = "buy" if buy else "sell"
    entry = close1
    indicator_profile = extra_diagnostics.get("indicator_exit_profile") or INDICATOR_SETUP_PROFILES.get(trigger_mode)
    if indicator_profile:
        profile_atr = atr5 if str(indicator_profile["timeframe"]) == "M5" else atr1
        sl_dist = min(12.0, max(1.50, profile_atr * float(indicator_profile["sl_atr"])))
        tp1_dist = max(1.0, sl_dist * float(indicator_profile["tp_r"]))
        tp2_dist = tp1_dist
        be_trigger_dist = sl_dist * float(indicator_profile["be_r"])
        be_buffer_dist = float(indicator_profile["be_buffer"])
        max_hold_minutes = float(indicator_profile["hold_minutes"])
    elif _env_bool("XAU_SCALP_ADAPTIVE_LEVELS_ENABLED", False):
        min_sl = _env_float("XAU_SCALP_ADAPTIVE_MIN_SL_USD", 2.8)
        min_tp1 = _env_float("XAU_SCALP_ADAPTIVE_MIN_TP1_USD", 0.8)
        min_tp2 = _env_float("XAU_SCALP_ADAPTIVE_MIN_TP2_USD", min_tp1 + 0.4)
        sl_mult = _env_float("XAU_SCALP_ADAPTIVE_SL_ATR_MULT", 2.1)
        tp1_mult = _env_float("XAU_SCALP_ADAPTIVE_TP1_ATR_MULT", 1.05)
        tp2_mult = _env_float("XAU_SCALP_ADAPTIVE_TP2_ATR_MULT", 1.7)
        sl_dist = min(float(cfg.xau_scalp_sl_usd), max(min_sl, atr1 * sl_mult, atr5 * 0.45))
        tp1_dist = max(min_tp1, min(float(cfg.xau_scalp_tp1_usd), max(atr1 * tp1_mult, atr5 * 0.22)))
        tp2_dist = max(tp1_dist + 0.4, min(float(cfg.xau_scalp_tp2_usd), max(min_tp2, atr1 * tp2_mult, atr5 * 0.36)))
        be_trigger_dist = float(cfg.xau_scalp_be_trigger_usd)
        be_buffer_dist = float(cfg.xau_scalp_be_buffer_usd)
        max_hold_minutes = 0.0
    else:
        sl_dist = min(float(cfg.xau_scalp_sl_usd), max(3.0, atr1 * 2.2))
        tp1_dist = float(cfg.xau_scalp_tp1_usd)
        tp2_dist = float(cfg.xau_scalp_tp2_usd)
        be_trigger_dist = float(cfg.xau_scalp_be_trigger_usd)
        be_buffer_dist = float(cfg.xau_scalp_be_buffer_usd)
        max_hold_minutes = 0.0
    sl_dist *= min(3.0, max(0.25, _env_float("XAU_SCALP_SL_DISTANCE_MULTIPLIER", 1.0)))
    tp_multiplier = min(2.0, max(0.25, _env_float("XAU_SCALP_TP_DISTANCE_MULTIPLIER", 1.0)))
    tp1_dist *= tp_multiplier
    tp2_dist *= tp_multiplier
    be_trigger_dist *= min(
        2.0,
        max(0.0, _env_float("XAU_SCALP_BE_TRIGGER_MULTIPLIER", 1.0)),
    )
    if side == "buy":
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
        tp2 = entry + tp2_dist
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
        tp2 = entry - tp2_dist
    return {
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr1": atr1,
        "atr5": atr5,
        "setup_tag": setup_tag,
        "be_trigger_usd": be_trigger_dist,
        "be_buffer_usd": be_buffer_dist,
        "be_enabled": be_trigger_dist > 0.0,
        "max_hold_minutes": max_hold_minutes,
    }, diagnostics


def _session_active(ts: pd.Timestamp) -> bool:
    if not _env_bool("XAU_SCALP_SESSION_FILTER_ENABLED", False):
        return True
    raw = str(os.getenv("XAU_SCALP_ACTIVE_UTC_SESSIONS", "06:00-16:30") or "").strip()
    if not raw:
        return True
    minutes_now = int(ts.hour) * 60 + int(ts.minute)
    for chunk in raw.split(","):
        if "-" not in chunk:
            continue
        start_raw, end_raw = [part.strip() for part in chunk.split("-", 1)]
        try:
            start_h, start_m = [int(part) for part in start_raw.split(":", 1)]
            end_h, end_m = [int(part) for part in end_raw.split(":", 1)]
        except Exception:
            continue
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end and start <= minutes_now <= end:
            return True
        if start > end and (minutes_now >= start or minutes_now <= end):
            return True
    return False


def _side_quality_blocked(trades: list[dict[str, Any]], side: str, ts: pd.Timestamp) -> bool:
    if not _env_bool("XAU_SCALP_SIDE_QUALITY_BLOCK_ENABLED", True):
        return False
    today = ts.date().isoformat()
    side_rows = [row for row in trades if row["side"] == side and str(row["closed"]).startswith(today)]
    min_trades = int(_env_float("XAU_SCALP_SIDE_QUALITY_MIN_TRADES", 4))
    if len(side_rows) < min_trades:
        return False
    wins = sum(1 for row in side_rows if float(row.get("profit", 0.0) or 0.0) > 0)
    losses = sum(1 for row in side_rows if float(row.get("profit", 0.0) or 0.0) < 0)
    win_rate = wins / max(1, wins + losses) * 100.0
    if win_rate >= _env_float("XAU_SCALP_SIDE_QUALITY_MIN_WIN_RATE", 35.0):
        return False
    try:
        last_closed = pd.Timestamp(side_rows[-1]["closed"])
    except Exception:
        return True
    block_seconds = _env_float("XAU_SCALP_SIDE_QUALITY_BLOCK_SECONDS", 1800.0)
    return (ts - last_closed).total_seconds() < block_seconds


def _lot_for_equity(
    symbol: str,
    cfg: Any,
    equity: float,
    reference_equity: float,
    stop_distance: float = 0.0,
) -> tuple[float, float]:
    leg_count = max(1, min(8, int(os.getenv("XAU_SCALP_LEG_COUNT", "3") or 3)))
    risk_pct = max(0.0, _env_float("XAU_SCALP_RISK_PCT", 0.0))
    per_leg_pct = max(0.0, _env_float("XAU_SCALP_RISK_PCT_PER_LEG", 0.0))
    if max(risk_pct, per_leg_pct) > 0.0 and stop_distance > 0.0:
        risk_per_leg = float(equity) * (per_leg_pct if per_leg_pct > 0.0 else risk_pct / leg_count) / 100.0
        loss_per_lot = abs(_profit(symbol, "buy", 1.0, 0.0, -float(stop_distance)))
        minimum_leg_lot = max(float(cfg.min_lot), _env_float("XAU_SCALP_MIN_LEG_LOT", float(cfg.min_lot)))
        leg = normalize_volume(
            symbol,
            max(minimum_leg_lot, risk_per_leg / loss_per_lot),
            float(cfg.min_lot),
            float(cfg.max_lot),
        )
        return leg * leg_count, leg
    if not bool(getattr(cfg, "xau_scalp_dynamic_lot_enabled", False)):
        total = float(cfg.xau_scalp_lot)
    else:
        step = max(1.0, float(getattr(cfg, "xau_scalp_dynamic_step_usd", 1000.0) or 1000.0))
        add = max(0.0, float(getattr(cfg, "xau_scalp_dynamic_lot_add", 0.01) or 0.01))
        basis = str(os.getenv("XAU_SCALP_DYNAMIC_LOT_BASIS", "profit") or "profit").strip().lower()
        if basis == "balance_per_leg":
            steps = max(1, int(max(0.0, float(equity or 0.0)) // step))
            total = max(float(cfg.min_lot), steps * add) * leg_count
        elif basis == "balance":
            steps = max(1, int(max(0.0, float(equity or 0.0)) // step))
            total = max(float(cfg.xau_scalp_lot), steps * add)
        else:
            steps = int(max(0.0, float(equity or 0.0) - float(reference_equity or 0.0)) // step)
            total = float(cfg.xau_scalp_lot) + (steps * add)
        total = min(total, float(getattr(cfg, "xau_scalp_dynamic_max_lot", cfg.max_lot) or cfg.max_lot))
    total = normalize_volume(symbol, total, float(cfg.min_lot), float(cfg.max_lot))
    leg = normalize_volume(symbol, total / leg_count, float(cfg.min_lot), float(cfg.max_lot))
    return total, leg


def _target_plan() -> list[str]:
    count = max(1, min(8, int(os.getenv("XAU_SCALP_LEG_COUNT", "3") or 3)))
    raw = os.getenv("XAU_SCALP_TARGET_PLAN", "tp1,tp2,tp2")
    plan = [part.strip().lower() for part in raw.split(",") if part.strip().lower() in {"tp1", "tp2"}]
    if not plan:
        plan = ["tp1", "tp2", "tp2"]
    while len(plan) < count:
        plan.append(plan[-1])
    return plan[:count]


def _target_r_plan() -> list[float]:
    count = max(1, min(8, int(os.getenv("XAU_SCALP_LEG_COUNT", "3") or 3)))
    raw = str(os.getenv("XAU_SCALP_TARGET_R_PLAN", "") or "").strip()
    if not raw:
        return []
    values: list[float] = []
    for part in raw.split(","):
        try:
            value = float(part.strip().lower().removesuffix("r"))
        except ValueError:
            continue
        if value > 0.0:
            values.append(min(12.0, value))
    if not values:
        return []
    while len(values) < count:
        values.append(values[-1])
    return values[:count]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--strategy-env", default="")
    parser.add_argument("--days", type=float, default=28.0)
    parser.add_argument("--sessions", type=int, default=0)
    parser.add_argument("--include-today", action="store_true")
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--min-hold-seconds", type=float, default=0.0)
    parser.add_argument("--spread-price", type=float, default=-1.0)
    parser.add_argument("--dynamic-spread", action="store_true")
    parser.add_argument("--commission-per-001", type=float, default=0.0)
    parser.add_argument("--sl-distance-multiplier", type=float, default=1.0)
    parser.add_argument("--add-adverse-usd", type=float, default=0.0)
    parser.add_argument("--add-legs", type=int, default=0)
    parser.add_argument("--add-target-mode", choices=("original", "average"), default="original")
    parser.add_argument("--protect-leg-count", type=int, default=-1)
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Temporary environment override, repeatable. Does not edit the profile file.",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    if args.strategy_env:
        load_dotenv(args.strategy_env, override=True)
    for raw in args.override:
        if "=" not in str(raw):
            raise ValueError(f"Invalid --override value: {raw}")
        key, value = str(raw).split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --override key: {raw}")
        os.environ[key] = value.strip()
    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    current_spread_price = max(0.0, float(tick.ask) - float(tick.bid)) if tick else 0.0
    spread_price = current_spread_price if float(args.spread_price) < 0 else max(0.0, float(args.spread_price))
    point = float(getattr(info, "point", 0.01) or 0.01)
    contract_size = max(0.0, float(getattr(info, "trade_contract_size", 0.0) or 0.0))

    now_at = datetime.now(UTC)
    end_at = now_at
    requested_sessions = max(0, int(args.sessions))
    selection_cutoff = datetime.combine(now_at.date(), datetime.min.time(), tzinfo=UTC)
    if requested_sessions and not args.include_today:
        end_at = datetime.combine(end_at.date(), datetime.min.time(), tzinfo=UTC)
    fetch_days = max(float(args.days), requested_sessions * 1.65 + 12.0)
    start_at = end_at - timedelta(days=fetch_days)
    warmup = timedelta(days=4)
    m1 = _rates(symbol, "M1", start_at - warmup, end_at + timedelta(hours=4))
    m5 = _rates(symbol, "M5", start_at - warmup, end_at + timedelta(hours=4))
    m15 = _rates(symbol, "M15", start_at - warmup, end_at + timedelta(hours=4))
    if m1.empty or m5.empty or m15.empty:
        raise RuntimeError("No rates returned from MT5")
    m1 = _align_frames(m1, m5, m15)
    session_dates: list[str] = []
    if requested_sessions:
        # XAU's broker day rolls around 22:00 UTC in the summer. Shifting two
        # hours maps the Sunday open into Monday and avoids counting it twice.
        session_key = (m1["time"] + pd.Timedelta(hours=2)).dt.date
        counts = m1.loc[m1["time"] < _ts(selection_cutoff)].groupby(session_key).size()
        completed = [day for day, count in counts.items() if int(count) >= 180]
        if len(completed) < requested_sessions:
            raise RuntimeError(f"Only {len(completed)} completed sessions available; requested {requested_sessions}")
        selected = completed[-requested_sessions:]
        session_dates = [day.isoformat() for day in selected]
        if args.include_today:
            current_session = (pd.Timestamp(now_at) + pd.Timedelta(hours=2)).date().isoformat()
            session_dates.append(f"{current_session}:partial")
        selected_mask = session_key.isin(selected)
        start_at = m1.loc[selected_mask, "time"].iloc[0].to_pydatetime()

    balance = float(args.start_balance)
    peak = balance
    max_dd = 0.0
    day_key = ""
    day_start_balance = balance
    day_peak_profit = 0.0
    daily_profit_locked = False
    cooldown = max(0, int(getattr(cfg, "xau_scalp_cooldown_seconds", 300)))
    next_allowed = _ts(start_at)
    open_legs: list[Leg] = []
    trades: list[dict[str, Any]] = []
    batches = 0
    triggers = {"buy": 0, "sell": 0}
    skipped = 0
    skip_reasons: dict[str, int] = {}
    spread_paid = 0.0
    commission_paid = 0.0
    scale_ins = 0
    scale_in_done = False

    start_idx = _search_time(m1, start_at, side="left")
    for idx in range(max(80, start_idx), len(m1)):
        ts = m1.iloc[idx]["time"]
        if ts > _ts(end_at):
            break
        current_day = ts.date().isoformat()
        if current_day != day_key:
            day_key = current_day
            day_start_balance = balance
            day_peak_profit = 0.0
            daily_profit_locked = False
        row = m1.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])

        add_adverse = max(0.0, float(args.add_adverse_usd))
        add_legs = max(0, int(args.add_legs))
        if open_legs and not scale_in_done and add_adverse > 0.0 and add_legs > 0:
            base_leg = open_legs[0]
            add_entry = base_leg.entry - add_adverse if base_leg.side == "buy" else base_leg.entry + add_adverse
            add_hit = low <= add_entry if base_leg.side == "buy" else high >= add_entry
            if add_hit:
                original_tp = float(base_leg.tp)
                additions = [
                    Leg(
                        base_leg.side,
                        add_entry,
                        base_leg.sl,
                        original_tp,
                        f"add{i + 1}",
                        idx,
                        setup_tag=base_leg.setup_tag,
                        lot=base_leg.lot,
                        spread_price=base_leg.spread_price,
                        be_trigger=base_leg.be_trigger,
                        be_buffer=base_leg.be_buffer,
                        be_enabled=base_leg.be_enabled,
                        max_hold_until_idx=base_leg.max_hold_until_idx,
                    )
                    for i in range(add_legs)
                ]
                if args.add_target_mode == "average":
                    average_entry = (
                        sum(float(leg.entry) for leg in open_legs) + (add_entry * add_legs)
                    ) / (len(open_legs) + add_legs)
                    tp_distance = abs(original_tp - float(base_leg.entry))
                    average_tp = average_entry + tp_distance if base_leg.side == "buy" else average_entry - tp_distance
                    for leg in open_legs:
                        leg.tp = average_tp
                    for leg in additions:
                        leg.tp = average_tp
                open_legs.extend(additions)
                scale_in_done = True
                scale_ins += 1

        protect_leg_count = int(args.protect_leg_count)
        if protect_leg_count < 0:
            protect_leg_count = max(0, int(_env_float("XAU_SCALP_PROTECT_LEG_COUNT", len(open_legs))))
        batch_leg_counts: dict[int, int] = {}
        for leg in list(open_legs):
            leg_index = batch_leg_counts.get(leg.opened_idx, 0)
            batch_leg_counts[leg.opened_idx] = leg_index + 1
            if leg.status != "open":
                continue
            if leg.min_hold_until_idx >= 0 and idx < leg.min_hold_until_idx:
                continue
            be_trigger = float(leg.be_trigger)
            be_buffer = float(leg.be_buffer)
            advance = high - leg.entry if leg.side == "buy" else leg.entry - low
            # M1 cannot reveal intrabar ordering: an already active stop wins ties.
            if _hit_sl(leg.side, leg.sl, high, low):
                leg.status = "be" if abs(leg.sl - leg.entry) < 0.05 else "loss"
                leg.exit = leg.sl
                leg.closed_idx = idx
                continue
            if leg.be_enabled and be_trigger > 0.0 and leg_index < protect_leg_count and advance >= be_trigger:
                candidate = leg.entry + be_buffer if leg.side == "buy" else leg.entry - be_buffer
                if _env_bool("XAU_SCALP_TRAIL_ENABLED", True):
                    trail_start = _env_float("XAU_SCALP_TRAIL_START_USD", 3.0)
                    trail_step = max(0.25, _env_float("XAU_SCALP_TRAIL_STEP_USD", 1.0))
                    trail_buffer = max(be_buffer, _env_float("XAU_SCALP_TRAIL_BE_BUFFER_USD", 0.15))
                    if advance >= max(be_trigger, trail_start):
                        locked = trail_buffer + (int((advance - trail_start) // trail_step) * trail_step)
                        candidate = leg.entry + locked if leg.side == "buy" else leg.entry - locked
                leg.sl = _better_stop(leg.side, leg.sl, candidate)
            if _hit_tp(leg.side, leg.tp, high, low):
                leg.status = "win"
                leg.exit = leg.tp
                leg.closed_idx = idx
            elif _hit_sl(leg.side, leg.sl, high, low):
                leg.status = "be" if abs(leg.sl - leg.entry) < 0.05 else "loss"
                leg.exit = leg.sl
                leg.closed_idx = idx
            elif leg.max_hold_until_idx >= 0 and idx >= leg.max_hold_until_idx:
                leg.status = "timeout"
                leg.exit = float(row["close"])
                leg.closed_idx = idx

        closed_now = [leg for leg in open_legs if leg.status != "open"]
        for leg in closed_now:
            profit_001 = _profit(symbol, leg.side, 0.01, leg.entry, leg.exit)
            leg_lot = float(leg.lot)
            if leg_lot <= 0.0:
                _total_lot, leg_lot = _lot_for_equity(symbol, cfg, balance, float(args.start_balance))
            applied_spread = float(leg.spread_price)
            spread_cost = abs(_profit(symbol, "buy", leg_lot, leg.entry, leg.entry + applied_spread)) if applied_spread > 0 else 0.0
            # One standard XAU 0.01 lot represents one ounce. Some 24/7 gold
            # products use a one-ounce contract and a minimum volume of 1.0,
            # so scaling by lot/0.01 would overstate both PnL and commission
            # one hundred times. MT5's profit calculator and contract size keep
            # the calculation valid for both contract specifications.
            commission_units = leg_lot * contract_size
            commission = max(0.0, float(args.commission_per_001)) * commission_units
            profit = _profit(symbol, leg.side, leg_lot, leg.entry, leg.exit) - spread_cost - commission
            balance += profit
            spread_paid += spread_cost
            commission_paid += commission
            peak = max(peak, balance)
            max_dd = min(max_dd, balance - peak)
            day_profit = balance - day_start_balance
            day_peak_profit = max(day_peak_profit, day_profit)
            trades.append(
                {
                    "opened": m1.iloc[leg.opened_idx]["time"].isoformat(),
                    "closed": ts.isoformat(),
                    "side": leg.side,
                    "leg": leg.leg,
                    "setup_tag": leg.setup_tag,
                    "status": leg.status,
                    "entry": round(leg.entry, 2),
                    "initial_sl": leg.initial_sl,
                    "initial_tp": leg.tp,
                    "exit": round(leg.exit, 2),
                    "profit_001": round(profit_001, 4),
                    "leg_lot": round(leg_lot, 2),
                    "spread_cost": round(spread_cost, 2),
                    "commission": round(commission, 2),
                    "profit": round(profit, 2),
                    "balance": round(balance, 2),
                }
            )
        open_legs = [leg for leg in open_legs if leg.status == "open"]

        if (open_legs and _env_bool("XAU_SCALP_SINGLE_ACTIVE_BATCH_ENABLED", False)) or ts < next_allowed:
            continue
        day_profit = balance - day_start_balance
        if _env_bool("XAU_SCALP_DAILY_PROFIT_LOCK_ENABLED", False):
            trigger = max(
                _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_USD", 0.0),
                day_start_balance * _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_BALANCE_PCT", 0.0) / 100.0,
            )
            giveback_usd = _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_USD", 0.0)
            giveback_pct = _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_PCT", 0.0)
            giveback_balance = (
                day_start_balance * _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_BALANCE_PCT", 0.0) / 100.0
            )
            hard_stop = _env_float("XAU_SCALP_DAILY_PROFIT_STOP_USD", 0.0)
            day_peak_profit = max(day_peak_profit, day_profit)
            if hard_stop > 0 and day_profit >= hard_stop:
                daily_profit_locked = True
            if trigger > 0 and day_peak_profit >= trigger:
                allowed_giveback = max(giveback_usd, day_peak_profit * giveback_pct, giveback_balance)
                if allowed_giveback > 0 and (day_peak_profit - day_profit) >= allowed_giveback:
                    daily_profit_locked = True
            if daily_profit_locked:
                skip_reasons["daily_profit_lock"] = skip_reasons.get("daily_profit_lock", 0) + 1
                continue
        if not _session_active(ts):
            skip_reasons["outside_active_session"] = skip_reasons.get("outside_active_session", 0) + 1
            continue
        signal, _diag = _signal_from_rows(m1, idx, cfg)
        if not signal:
            skipped += 1
            continue
        sl_multiplier = max(0.1, float(args.sl_distance_multiplier))
        original_sl_distance = abs(float(signal["entry"]) - float(signal["sl"]))
        widened_sl_distance = original_sl_distance * sl_multiplier
        signal["sl"] = (
            float(signal["entry"]) - widened_sl_distance
            if str(signal["side"]) == "buy"
            else float(signal["entry"]) + widened_sl_distance
        )
        if _side_quality_blocked(trades, str(signal["side"]), ts):
            skip_reasons["side_quality_block"] = skip_reasons.get("side_quality_block", 0) + 1
            continue
        tp1_dist = abs(float(signal["tp1"]) - float(signal["entry"]))
        max_spread_pct = _env_float("XAU_SCALP_MAX_SPREAD_TP1_PCT", 0.0)
        if max_spread_pct > 0 and tp1_dist > 0 and spread_price > tp1_dist * max_spread_pct:
            skip_reasons["spread_too_large_vs_tp1"] = skip_reasons.get("spread_too_large_vs_tp1", 0) + 1
            continue
        batches += 1
        triggers[signal["side"]] += 1
        next_allowed = ts + pd.Timedelta(seconds=cooldown)
        r_plan = _target_r_plan()
        if r_plan:
            entry = float(signal["entry"])
            stop_distance = abs(entry - float(signal["sl"]))
            direction = 1.0 if str(signal["side"]) == "buy" else -1.0
            targets = [
                (f"r{index + 1}_{target_r:g}", entry + (direction * stop_distance * target_r))
                for index, target_r in enumerate(r_plan)
            ]
        else:
            target_counts: dict[str, int] = {"tp1": 0, "tp2": 0}
            targets = []
            for target in _target_plan():
                target_counts[target] += 1
                suffix = "" if target_counts[target] == 1 else chr(ord("a") + target_counts[target] - 2)
                targets.append((f"{target}{suffix}", signal[target]))
        _total_lot, entry_leg_lot = _lot_for_equity(
            symbol,
            cfg,
            balance,
            float(args.start_balance),
            abs(float(signal["entry"]) - float(signal["sl"])),
        )
        entry_spread = float(row.get("spread", 0.0) or 0.0) * point if args.dynamic_spread else spread_price
        max_hold_minutes = float(signal.get("max_hold_minutes", 0.0) or 0.0)
        max_hold_idx = -1
        if max_hold_minutes > 0.0:
            max_hold_time = ts + pd.Timedelta(minutes=max_hold_minutes)
            max_hold_idx = min(_search_time(m1, max_hold_time, side="left"), len(m1) - 1)
        new_legs = [
            Leg(
                signal["side"],
                signal["entry"],
                signal["sl"],
                float(tp),
                leg,
                idx,
                setup_tag=str(signal.get("setup_tag", "")),
                lot=entry_leg_lot,
                spread_price=entry_spread,
                be_trigger=float(signal.get("be_trigger_usd", cfg.xau_scalp_be_trigger_usd) or 0.0),
                be_buffer=float(signal.get("be_buffer_usd", cfg.xau_scalp_be_buffer_usd) or 0.0),
                be_enabled=bool(signal.get("be_enabled", True)),
                max_hold_until_idx=max_hold_idx,
            )
            for leg, tp in targets
        ]
        scale_in_done = False
        if float(args.min_hold_seconds) > 0:
            hold_until = ts + pd.Timedelta(seconds=float(args.min_hold_seconds))
            hold_idx = min(int(_search_time(m1, hold_until, side="left")), len(m1) - 1)
            for leg in new_legs:
                leg.min_hold_until_idx = hold_idx
        open_legs.extend(new_legs)

    by_status: dict[str, int] = {}
    for row in trades:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    closed = len(trades)
    positive = [float(row["profit"]) for row in trades if float(row["profit"]) > 0.005]
    negative = [float(row["profit"]) for row in trades if float(row["profit"]) < -0.005]
    decided = len(positive) + len(negative)
    gross_profit = sum(positive)
    gross_loss = abs(sum(negative))
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "sessions_requested": requested_sessions,
        "sessions_included": session_dates,
        "symbol": symbol,
        "start_balance": round(float(args.start_balance), 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - float(args.start_balance), 2),
        "profit_percent": round(((balance / float(args.start_balance)) - 1.0) * 100.0, 2),
        "max_drawdown_from_peak": round(max_dd, 2),
        "spread_paid": round(spread_paid, 2),
        "commission_paid": round(commission_paid, 2),
        "spread_snapshot": {"price": round(spread_price, 5), "points": round(spread_price / point, 2)},
        "batches": batches,
        "scale_ins": scale_ins,
        "legs_closed": closed,
        "open_legs_at_end": len(open_legs),
        "win_rate_closed_legs_pct": round((len(positive) / decided * 100.0), 2) if decided else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0.0 else None,
        "average_win": round(gross_profit / max(1, len(positive)), 3),
        "average_loss": round(sum(negative) / max(1, len(negative)), 3),
        "by_status": by_status,
        "triggers": triggers,
        "skipped_no_signal_bars": skipped,
        "skip_reasons": skip_reasons,
        "settings": {
            "tp1": float(cfg.xau_scalp_tp1_usd),
            "tp2": float(cfg.xau_scalp_tp2_usd),
            "sl": float(cfg.xau_scalp_sl_usd),
            "be_trigger": float(cfg.xau_scalp_be_trigger_usd),
            "be_buffer": float(cfg.xau_scalp_be_buffer_usd),
            "trail_enabled": _env_bool("XAU_SCALP_TRAIL_ENABLED", True),
            "trail_start": _env_float("XAU_SCALP_TRAIL_START_USD", 3.0),
            "trail_step": _env_float("XAU_SCALP_TRAIL_STEP_USD", 1.0),
            "cooldown_seconds": cooldown,
            "dynamic_lot_enabled": bool(getattr(cfg, "xau_scalp_dynamic_lot_enabled", False)),
            "base_total_lot": float(cfg.xau_scalp_lot),
            "risk_pct_per_setup": _env_float("XAU_SCALP_RISK_PCT", 0.0),
            "risk_pct_per_leg": _env_float("XAU_SCALP_RISK_PCT_PER_LEG", 0.0),
            "minimum_leg_lot": _env_float("XAU_SCALP_MIN_LEG_LOT", float(cfg.min_lot)),
            "dynamic_step_usd": float(getattr(cfg, "xau_scalp_dynamic_step_usd", 0.0) or 0.0),
            "dynamic_lot_add": float(getattr(cfg, "xau_scalp_dynamic_lot_add", 0.0) or 0.0),
            "min_hold_seconds": float(args.min_hold_seconds),
            "adaptive_levels_enabled": _env_bool("XAU_SCALP_ADAPTIVE_LEVELS_ENABLED", False),
            "session_filter_enabled": _env_bool("XAU_SCALP_SESSION_FILTER_ENABLED", False),
            "active_sessions_utc": os.getenv("XAU_SCALP_ACTIVE_UTC_SESSIONS", ""),
            "daily_profit_lock_enabled": _env_bool("XAU_SCALP_DAILY_PROFIT_LOCK_ENABLED", False),
            "daily_profit_lock_trigger_balance_pct": _env_float(
                "XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_BALANCE_PCT", 0.0
            ),
            "daily_profit_lock_giveback_balance_pct": _env_float(
                "XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_BALANCE_PCT", 0.0
            ),
            "side_quality_block_enabled": _env_bool("XAU_SCALP_SIDE_QUALITY_BLOCK_ENABLED", True),
            "max_spread_tp1_pct": _env_float("XAU_SCALP_MAX_SPREAD_TP1_PCT", 0.0),
            "overrides": list(args.override),
            "sl_distance_multiplier": max(0.1, float(args.sl_distance_multiplier)),
            "dynamic_spread": bool(args.dynamic_spread),
            "commission_per_001": max(0.0, float(args.commission_per_001)),
            "include_today": bool(args.include_today),
        },
        "trades": trades,
    }
    out = Path(args.output) if args.output else cfg.data_dir / "xau_scalp_backtest_20trading_current.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(out)
    shutdown()


if __name__ == "__main__":
    main()
