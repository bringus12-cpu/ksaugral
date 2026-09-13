from __future__ import annotations

import os
from typing import Any


EXTRA_SETUP_MODES = {
    "ema_cross_fast",
    "trend_continuation_fast",
    "momentum_impulse",
    "bollinger_reclaim",
    "bb_keltner_macd_squeeze",
    "vwap_macd_reclaim",
    "bb_macd_reclaim_slow",
    "rsi_macd_reversal_slow",
    "bb_macd_breakout",
    "stoch_bb_reversion",
    "liquidity_sweep",
    "one_bar_breakout",
    "rsi_reversal",
    "volatility_expansion",
    "micro_ema_bounce",
    "micro_mean_revert",
    "accuracy_trend_reclaim",
    "accuracy_momentum_confirm",
    "accuracy_liquidity_sweep",
    "accuracy_range_reclaim",
    "accuracy_two_bar_continue",
    "accuracy_ema_rejection",
    "accuracy_adx_breakout",
    "accuracy_ensemble",
    "accuracy_selective_ensemble",
    "accuracy_multi_trigger",
    "smc_liquidity_sweep",
    "smc_bos_retest",
    "smc_fvg_retest",
    "smc_order_block",
    "smc_mss_retest",
    "smc_breaker_retest",
    "smc_discount_reclaim",
    "smc_displacement",
    "smc_ensemble",
    "smc_selective_ensemble",
    "smc_adaptive_ensemble",
    "gold_multi_strategy",
    "gold_multi_strategy_v2",
}


INDICATOR_SETUP_PROFILES: dict[str, dict[str, Any]] = {
    "bb_keltner_macd_squeeze": {
        "tag": "SC-BBKeltMACD",
        "timeframe": "M5",
        "sl_atr": 1.6,
        "tp_r": 1.8,
        "be_r": 1.0,
        "be_buffer": 0.10,
        "hold_minutes": 360,
        "cooldown_seconds": 900,
    },
    "vwap_macd_reclaim": {
        "tag": "SC-VWAPMACD",
        "timeframe": "M5",
        "sl_atr": 1.6,
        "tp_r": 1.8,
        "be_r": 1.0,
        "be_buffer": 0.10,
        "hold_minutes": 360,
        "cooldown_seconds": 900,
    },
    "bb_macd_reclaim_slow": {
        "tag": "SC-BBMACD-RCL",
        "timeframe": "M5",
        "sl_atr": 1.0,
        "tp_r": 0.9,
        "be_r": 0.55,
        "be_buffer": 0.08,
        "hold_minutes": 120,
        "cooldown_seconds": 300,
    },
    "rsi_macd_reversal_slow": {
        "tag": "SC-RSIMACD",
        "timeframe": "M5",
        "sl_atr": 1.3,
        "tp_r": 1.25,
        "be_r": 0.75,
        "be_buffer": 0.10,
        "hold_minutes": 240,
        "cooldown_seconds": 600,
    },
    "bb_macd_breakout": {
        "tag": "SC-BBMACD-BRK",
        "timeframe": "M5",
        "sl_atr": 2.0,
        "tp_r": 2.2,
        "be_r": 0.0,
        "be_buffer": 0.0,
        "hold_minutes": 480,
        "cooldown_seconds": 1800,
    },
    "stoch_bb_reversion": {
        "tag": "SC-STOCHBB",
        "timeframe": "M1",
        "sl_atr": 2.0,
        "tp_r": 2.2,
        "be_r": 0.0,
        "be_buffer": 0.0,
        "hold_minutes": 480,
        "cooldown_seconds": 1800,
    },
}


ACCURACY_SETUP_TAGS = {
    "accuracy_trend_reclaim": "SC-TrendReclaim",
    "accuracy_momentum_confirm": "SC-Momentum",
    "accuracy_liquidity_sweep": "SC-LiqSweep",
    "accuracy_range_reclaim": "SC-RangeReclaim",
    "accuracy_two_bar_continue": "SC-TwoBar",
    "accuracy_ema_rejection": "SC-EmaReject",
    "accuracy_adx_breakout": "SC-AdxBreak",
}


SMC_SETUP_TAGS = {
    "smc_liquidity_sweep": "SMC-LiqSweep",
    "smc_bos_retest": "SMC-BOSRetest",
    "smc_fvg_retest": "SMC-FVGRetest",
    "smc_order_block": "SMC-OBRetest",
    "smc_mss_retest": "SMC-MSSRetest",
    "smc_breaker_retest": "SMC-Breaker",
    "smc_discount_reclaim": "SMC-Discount",
    "smc_displacement": "SMC-Displace",
}


def evaluate_extra_setup(
    mode: str,
    last1: Any,
    prev1: Any,
    last5: Any,
    prev5: Any,
    last15: Any,
    atr1: float,
    atr5: float,
    recent_move: float,
    prev2: Any | None = None,
    prev3: Any | None = None,
) -> tuple[bool, bool, dict[str, Any]]:
    if mode not in EXTRA_SETUP_MODES:
        return False, False, {}

    close = float(last1["close"])
    open_ = float(last1["open"])
    high = float(last1["high"])
    low = float(last1["low"])
    prev_close = float(prev1["close"])
    prev_open = float(prev1["open"])
    prev_body = abs(prev_close - prev_open)
    prev_range = max(0.00001, float(prev1["high"]) - float(prev1["low"]))
    prev_close_location = (prev_close - float(prev1["low"])) / prev_range
    body = abs(close - open_)
    candle_range = max(0.00001, high - low)
    close_location = (close - low) / candle_range
    ema1 = float(last1["ema20"])
    prev_ema1 = float(prev1["ema20"])
    rsi = float(last1["rsi14"])
    prev_rsi = float(prev1["rsi14"])
    volume_ratio = float(last1["tick_volume"]) / max(float(last1["volume_ma20"]), 1.0)
    m5_up = float(last5["ema20"]) > float(last5["ema50"])
    m5_down = float(last5["ema20"]) < float(last5["ema50"])
    m15_up = float(last15["ema20"]) > float(last15["ema50"])
    m15_down = float(last15["ema20"]) < float(last15["ema50"])
    buy = sell = False

    if mode in INDICATOR_SETUP_PROFILES:
        profile = dict(INDICATOR_SETUP_PROFILES[mode])
        if mode == "bb_macd_breakout":
            profile["sl_atr"] = float(os.getenv("SC_BBMACD_EXIT_SL_ATR", str(profile["sl_atr"])) or profile["sl_atr"])
            profile["tp_r"] = float(os.getenv("SC_BBMACD_EXIT_TP_R", str(profile["tp_r"])) or profile["tp_r"])
            profile["be_r"] = float(os.getenv("SC_BBMACD_EXIT_BE_R", str(profile["be_r"])) or profile["be_r"])
            profile["be_buffer"] = float(
                os.getenv("SC_BBMACD_EXIT_BE_BUFFER", str(profile["be_buffer"])) or profile["be_buffer"]
            )
            profile["hold_minutes"] = float(
                os.getenv("SC_BBMACD_EXIT_HOLD_MINUTES", str(profile["hold_minutes"])) or profile["hold_minutes"]
            )
        m5_close = float(last5["close"])
        m5_open = float(last5["open"])
        m5_low = float(last5["low"])
        m5_high = float(last5["high"])
        previous_m5_close = float(prev5["close"])
        m5_volume_ratio = float(last5["tick_volume"]) / max(float(last5["volume_ma20"]), 1.0)

        if mode == "bb_keltner_macd_squeeze":
            m5_hist = float(last5["macd_fast_hist"])
            previous_hist = float(prev5["macd_fast_hist"])
            was_squeezed = bool(prev5["bb_keltner_fast_squeeze"])
            squeeze_released = not bool(last5["bb_keltner_fast_squeeze"])
            require_m5_trend = str(os.getenv("SC_BBKELT_REQUIRE_M5_TREND", "false") or "").lower() in {
                "1", "true", "yes", "on"
            }
            require_m15_trend = str(os.getenv("SC_BBKELT_REQUIRE_M15_TREND", "false") or "").lower() in {
                "1", "true", "yes", "on"
            }
            buy_trend_ok = (not require_m5_trend or m5_up) and (not require_m15_trend or m15_up)
            sell_trend_ok = (not require_m5_trend or m5_down) and (not require_m15_trend or m15_down)
            buy = (
                was_squeezed and squeeze_released
                and m5_close > float(last5["bb_fast_upper"])
                and m5_hist > 0.0 and m5_hist > previous_hist
                and m5_volume_ratio >= 0.9
                and buy_trend_ok
            )
            sell = (
                was_squeezed and squeeze_released
                and m5_close < float(last5["bb_fast_lower"])
                and m5_hist < 0.0 and m5_hist < previous_hist
                and m5_volume_ratio >= 0.9
                and sell_trend_ok
            )
        elif mode == "vwap_macd_reclaim":
            m5_hist = float(last5["macd_fast_hist"])
            previous_hist = float(prev5["macd_fast_hist"])
            buy = (
                m5_up and previous_m5_close <= float(prev5["vwap"])
                and m5_close > float(last5["vwap"])
                and m5_hist > previous_hist and m5_close > m5_open
            )
            sell = (
                m5_down and previous_m5_close >= float(prev5["vwap"])
                and m5_close < float(last5["vwap"])
                and m5_hist < previous_hist and m5_close < m5_open
            )
        elif mode == "bb_macd_reclaim_slow":
            m5_hist = float(last5["macd_slow_hist"])
            previous_hist = float(prev5["macd_slow_hist"])
            buy = (
                m5_low <= float(last5["bb_slow_lower"])
                and m5_close > float(last5["bb_slow_lower"])
                and m5_close > m5_open and m5_hist > previous_hist
                and float(last5["rsi14"]) <= 42.0
                and float(last5["close_location"]) >= 0.58
            )
            sell = (
                m5_high >= float(last5["bb_slow_upper"])
                and m5_close < float(last5["bb_slow_upper"])
                and m5_close < m5_open and m5_hist < previous_hist
                and float(last5["rsi14"]) >= 58.0
                and float(last5["close_location"]) <= 0.42
            )
        elif mode == "rsi_macd_reversal_slow":
            m5_hist = float(last5["macd_slow_hist"])
            previous_hist = float(prev5["macd_slow_hist"])
            buy = (
                float(prev5["rsi14"]) <= 30.0
                and float(last5["rsi14"]) > float(prev5["rsi14"])
                and m5_hist > previous_hist
                and float(last5["bb_slow_percent_b"]) <= 0.35
                and m5_close > m5_open
            )
            sell = (
                float(prev5["rsi14"]) >= 70.0
                and float(last5["rsi14"]) < float(prev5["rsi14"])
                and m5_hist < previous_hist
                and float(last5["bb_slow_percent_b"]) >= 0.65
                and m5_close < m5_open
            )
        elif mode == "bb_macd_breakout":
            m5_hist = float(last5["macd_hist"])
            previous_hist = float(prev5["macd_hist"])
            squeeze_ratio = float(os.getenv("SC_BBMACD_SQUEEZE_RATIO", "0.85") or 0.85)
            min_volume_ratio = float(os.getenv("SC_BBMACD_MIN_VOLUME_RATIO", "0.9") or 0.9)
            close_location_limit = float(os.getenv("SC_BBMACD_CLOSE_LOCATION", "0.70") or 0.70)
            require_m5_trend = str(os.getenv("SC_BBMACD_REQUIRE_M5_TREND", "false") or "").lower() in {
                "1", "true", "yes", "on"
            }
            require_m15_trend = str(os.getenv("SC_BBMACD_REQUIRE_M15_TREND", "false") or "").lower() in {
                "1", "true", "yes", "on"
            }
            was_narrow = float(prev5["bb_width"]) <= float(prev5["bb_width_median"]) * squeeze_ratio
            buy_trend_ok = (not require_m5_trend or m5_up) and (not require_m15_trend or m15_up)
            sell_trend_ok = (not require_m5_trend or m5_down) and (not require_m15_trend or m15_down)
            buy = (
                was_narrow and previous_m5_close <= float(prev5["bb_upper"])
                and m5_close > float(last5["bb_upper"])
                and m5_hist > 0.0 and m5_hist > previous_hist
                and m5_volume_ratio >= min_volume_ratio
                and float(last5["close_location"]) >= close_location_limit
                and buy_trend_ok
            )
            sell = (
                was_narrow and previous_m5_close >= float(prev5["bb_lower"])
                and m5_close < float(last5["bb_lower"])
                and m5_hist < 0.0 and m5_hist < previous_hist
                and m5_volume_ratio >= min_volume_ratio
                and float(last5["close_location"]) <= 1.0 - close_location_limit
                and sell_trend_ok
            )
        else:
            m5_hist = float("nan")
            previous_hist = float("nan")
            buy = (
                float(prev1["stoch_k"]) <= 25.0
                and float(prev1["stoch_k"]) <= float(prev1["stoch_d"])
                and float(last1["stoch_k"]) > float(last1["stoch_d"])
                and low <= float(last1["bb_lower"]) + atr1 * 0.15
                and close > open_
            )
            sell = (
                float(prev1["stoch_k"]) >= 75.0
                and float(prev1["stoch_k"]) >= float(prev1["stoch_d"])
                and float(last1["stoch_k"]) < float(last1["stoch_d"])
                and high >= float(last1["bb_upper"]) - atr1 * 0.15
                and close < open_
            )

        return buy, sell, {
            "extra_setup": mode,
            "setup_tag": str(profile["tag"]),
            "signal_timeframe": str(profile["timeframe"]),
            "indicator_exit_profile": dict(profile),
            "macd_hist": round(m5_hist, 6) if m5_hist == m5_hist else None,
            "previous_macd_hist": round(previous_hist, 6) if previous_hist == previous_hist else None,
            "bb_macd_filters": {
                "squeeze_ratio": squeeze_ratio,
                "min_volume_ratio": min_volume_ratio,
                "close_location": close_location_limit,
                "require_m5_trend": require_m5_trend,
                "require_m15_trend": require_m15_trend,
            } if mode == "bb_macd_breakout" else None,
            "bb_keltner_filters": {
                "require_m5_trend": require_m5_trend,
                "require_m15_trend": require_m15_trend,
            } if mode == "bb_keltner_macd_squeeze" else None,
            "m5_volume_ratio": round(m5_volume_ratio, 4),
        }

    def accuracy_setup(accuracy_mode: str) -> tuple[bool, bool]:
        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)
        m5_close = float(last5["close"])
        m5_ema20 = float(last5["ema20"])
        m5_ema50 = float(last5["ema50"])
        m5_adx = float(last5["adx14"])
        ema_separation = abs(m5_ema20 - m5_ema50) / max(atr5, 0.00001)

        if accuracy_mode == "accuracy_trend_reclaim":
            setup_buy = (
                m15_up and m5_up and m5_close > m5_ema20 and m5_adx >= 18.0
                and prev_close <= prev_ema1 + atr1 * 0.12 and close > ema1
                and close > open_ and body >= atr1 * 0.25 and close_location >= 0.68
                and 50.0 <= rsi <= 68.0 and volume_ratio >= 0.8
                and recent_move <= atr1 * 1.4
            )
            setup_sell = (
                m15_down and m5_down and m5_close < m5_ema20 and m5_adx >= 18.0
                and prev_close >= prev_ema1 - atr1 * 0.12 and close < ema1
                and close < open_ and body >= atr1 * 0.25 and close_location <= 0.32
                and 32.0 <= rsi <= 50.0 and volume_ratio >= 0.8
                and recent_move >= -(atr1 * 1.4)
            )
            return setup_buy, setup_sell

        if accuracy_mode == "accuracy_momentum_confirm":
            setup_buy = (
                m15_up and m5_up and m5_adx >= 20.0 and close > float(prev1["high"])
                and close > open_ and atr1 * 0.5 <= body <= atr1 * 1.35
                and close_location >= 0.78 and volume_ratio >= 1.05
                and rsi >= 54.0 and recent_move <= atr1 * 1.9
            )
            setup_sell = (
                m15_down and m5_down and m5_adx >= 20.0 and close < float(prev1["low"])
                and close < open_ and atr1 * 0.5 <= body <= atr1 * 1.35
                and close_location <= 0.22 and volume_ratio >= 1.05
                and rsi <= 46.0 and recent_move >= -(atr1 * 1.9)
            )
            return setup_buy, setup_sell

        if accuracy_mode == "accuracy_liquidity_sweep":
            setup_buy = (
                m15_up and low < float(last1["ll20"]) and close > float(last1["ll20"])
                and close > open_ and lower_wick >= max(body * 0.8, atr1 * 0.2)
                and close_location >= 0.62 and rsi <= 48.0 and volume_ratio >= 0.8
            )
            setup_sell = (
                m15_down and high > float(last1["hh20"]) and close < float(last1["hh20"])
                and close < open_ and upper_wick >= max(body * 0.8, atr1 * 0.2)
                and close_location <= 0.38 and rsi >= 52.0 and volume_ratio >= 0.8
            )
            return setup_buy, setup_sell

        if accuracy_mode == "accuracy_range_reclaim":
            range_regime = m5_adx <= 17.0 and ema_separation <= 0.5
            setup_buy = (
                range_regime and low <= float(last1["bb_lower"]) and close > float(last1["bb_lower"])
                and close > open_ and lower_wick >= max(body * 0.65, atr1 * 0.15)
                and close_location >= 0.6 and rsi <= 36.0 and volume_ratio >= 0.7
            )
            setup_sell = (
                range_regime and high >= float(last1["bb_upper"]) and close < float(last1["bb_upper"])
                and close < open_ and upper_wick >= max(body * 0.65, atr1 * 0.15)
                and close_location <= 0.4 and rsi >= 64.0 and volume_ratio >= 0.7
            )
            return setup_buy, setup_sell

        if accuracy_mode == "accuracy_two_bar_continue":
            setup_buy = (
                m15_up and m5_up and m5_close > m5_ema20 and m5_adx >= 18.0
                and prev_close > prev_open and close > open_ and close > float(prev1["high"])
                and atr1 * 0.18 <= prev_body <= atr1 * 0.9
                and atr1 * 0.25 <= body <= atr1 * 1.1
                and prev_close_location >= 0.6 and close_location >= 0.72
                and 52.0 <= rsi <= 70.0 and volume_ratio >= 0.85
                and recent_move <= atr1 * 1.8
            )
            setup_sell = (
                m15_down and m5_down and m5_close < m5_ema20 and m5_adx >= 18.0
                and prev_close < prev_open and close < open_ and close < float(prev1["low"])
                and atr1 * 0.18 <= prev_body <= atr1 * 0.9
                and atr1 * 0.25 <= body <= atr1 * 1.1
                and prev_close_location <= 0.4 and close_location <= 0.28
                and 30.0 <= rsi <= 48.0 and volume_ratio >= 0.85
                and recent_move >= -(atr1 * 1.8)
            )
            return setup_buy, setup_sell

        if accuracy_mode == "accuracy_ema_rejection":
            setup_buy = (
                m15_up and m5_up and m5_close > m5_ema20 and m5_adx >= 16.0
                and low <= ema1 + atr1 * 0.08 and close > ema1 and close > open_
                and lower_wick >= max(body * 0.4, atr1 * 0.12)
                and close_location >= 0.65 and 48.0 <= rsi <= 66.0
                and volume_ratio >= 0.75 and recent_move <= atr1 * 1.3
            )
            setup_sell = (
                m15_down and m5_down and m5_close < m5_ema20 and m5_adx >= 16.0
                and high >= ema1 - atr1 * 0.08 and close < ema1 and close < open_
                and upper_wick >= max(body * 0.4, atr1 * 0.12)
                and close_location <= 0.35 and 34.0 <= rsi <= 52.0
                and volume_ratio >= 0.75 and recent_move >= -(atr1 * 1.3)
            )
            return setup_buy, setup_sell

        if accuracy_mode == "accuracy_adx_breakout":
            strong_adx = m5_adx >= 22.0
            setup_buy = (
                strong_adx and m15_up and m5_up and close > float(prev1["high"])
                and close > open_ and atr1 * 0.3 <= body <= atr1 * 1.25
                and close_location >= 0.7 and 51.0 <= rsi <= 74.0
                and volume_ratio >= 0.85 and recent_move <= atr1 * 2.2
            )
            setup_sell = (
                strong_adx and m15_down and m5_down and close < float(prev1["low"])
                and close < open_ and atr1 * 0.3 <= body <= atr1 * 1.25
                and close_location <= 0.3 and 26.0 <= rsi <= 49.0
                and volume_ratio >= 0.85 and recent_move >= -(atr1 * 2.2)
            )
            return setup_buy, setup_sell
        return False, False

    def smc_setup(smc_mode: str) -> tuple[bool, bool]:
        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)
        m5_close = float(last5["close"])
        m5_ema20 = float(last5["ema20"])
        m5_ema50 = float(last5["ema50"])
        m5_adx = float(last5["adx14"])

        if smc_mode == "smc_liquidity_sweep":
            swept_low = float(last1["ll20"])
            swept_high = float(last1["hh20"])
            setup_buy = (
                m15_up and m5_close >= m5_ema50
                and low < swept_low and close > swept_low and close > open_
                and lower_wick >= max(body * 0.55, atr1 * 0.18)
                and close_location >= 0.58 and 35.0 <= rsi <= 56.0
                and volume_ratio >= 0.75 and recent_move <= atr1 * 1.4
            )
            setup_sell = (
                m15_down and m5_close <= m5_ema50
                and high > swept_high and close < swept_high and close < open_
                and upper_wick >= max(body * 0.55, atr1 * 0.18)
                and close_location <= 0.42 and 44.0 <= rsi <= 65.0
                and volume_ratio >= 0.75 and recent_move >= -(atr1 * 1.4)
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_bos_retest":
            broken_high = float(prev1["hh20"])
            broken_low = float(prev1["ll20"])
            setup_buy = (
                m15_up and m5_up and m5_adx >= 16.0
                and prev_close > broken_high + atr1 * 0.04
                and low <= broken_high + atr1 * 0.22 and close > broken_high
                and close > open_ and close_location >= 0.58
                and 49.0 <= rsi <= 70.0 and volume_ratio >= 0.7
                and recent_move <= atr1 * 1.7
            )
            setup_sell = (
                m15_down and m5_down and m5_adx >= 16.0
                and prev_close < broken_low - atr1 * 0.04
                and high >= broken_low - atr1 * 0.22 and close < broken_low
                and close < open_ and close_location <= 0.42
                and 30.0 <= rsi <= 51.0 and volume_ratio >= 0.7
                and recent_move >= -(atr1 * 1.7)
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_fvg_retest":
            if prev2 is None or prev3 is None:
                return False, False
            p2_open = float(prev2["open"])
            p2_close = float(prev2["close"])
            p2_body = abs(p2_close - p2_open)
            bull_gap_low = float(prev3["high"])
            bull_gap_high = float(prev1["low"])
            bear_gap_low = float(prev1["high"])
            bear_gap_high = float(prev3["low"])
            bull_gap = bull_gap_high - bull_gap_low
            bear_gap = bear_gap_high - bear_gap_low
            setup_buy = (
                m15_up and m5_up and p2_close > p2_open and p2_body >= atr1 * 0.5
                and bull_gap >= atr1 * 0.10
                and low <= bull_gap_high and close > (bull_gap_low + bull_gap_high) / 2.0
                and close > open_ and close_location >= 0.55
                and 47.0 <= rsi <= 69.0 and recent_move <= atr1 * 1.8
            )
            setup_sell = (
                m15_down and m5_down and p2_close < p2_open and p2_body >= atr1 * 0.5
                and bear_gap >= atr1 * 0.10
                and high >= bear_gap_low and close < (bear_gap_low + bear_gap_high) / 2.0
                and close < open_ and close_location <= 0.45
                and 31.0 <= rsi <= 53.0 and recent_move >= -(atr1 * 1.8)
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_order_block":
            if prev2 is None or prev3 is None:
                return False, False
            p3_open = float(prev3["open"])
            p3_close = float(prev3["close"])
            p3_low = float(prev3["low"])
            p3_high = float(prev3["high"])
            p2_open = float(prev2["open"])
            p2_close = float(prev2["close"])
            p2_body = abs(p2_close - p2_open)
            bull_mid = (p3_open + p3_close) / 2.0
            bear_mid = bull_mid
            setup_buy = (
                m15_up and m5_up and p3_close < p3_open
                and p2_close > p2_open and p2_body >= atr1 * 0.65 and p2_close > p3_high
                and float(prev1["close"]) > p3_high
                and low <= max(p3_open, p3_close) and low >= p3_low - atr1 * 0.08
                and close > bull_mid and close > open_ and close_location >= 0.58
            )
            setup_sell = (
                m15_down and m5_down and p3_close > p3_open
                and p2_close < p2_open and p2_body >= atr1 * 0.65 and p2_close < p3_low
                and float(prev1["close"]) < p3_low
                and high >= min(p3_open, p3_close) and high <= p3_high + atr1 * 0.08
                and close < bear_mid and close < open_ and close_location <= 0.42
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_mss_retest":
            if prev2 is None:
                return False, False
            p2_low = float(prev2["low"])
            p2_high = float(prev2["high"])
            p2_ll20 = float(prev2["ll20"])
            p2_hh20 = float(prev2["hh20"])
            p1_open = float(prev1["open"])
            p1_close = float(prev1["close"])
            p1_body = abs(p1_close - p1_open)
            # Liquidity is swept first, then a displacement candle changes
            # short-term structure. The current candle must retest and hold it.
            setup_buy = (
                p2_low < p2_ll20 and p1_close > p2_high + atr1 * 0.04
                and p1_close > p1_open and p1_body >= atr1 * 0.45
                and low <= p2_high + atr1 * 0.20 and close > p2_high
                and close > open_ and close_location >= 0.60
                and m5_close >= m5_ema50 and m5_adx >= 14.0
                and 42.0 <= rsi <= 68.0 and volume_ratio >= 0.75
                and recent_move <= atr1 * 1.6
            )
            setup_sell = (
                p2_high > p2_hh20 and p1_close < p2_low - atr1 * 0.04
                and p1_close < p1_open and p1_body >= atr1 * 0.45
                and high >= p2_low - atr1 * 0.20 and close < p2_low
                and close < open_ and close_location <= 0.40
                and m5_close <= m5_ema50 and m5_adx >= 14.0
                and 32.0 <= rsi <= 58.0 and volume_ratio >= 0.75
                and recent_move >= -(atr1 * 1.6)
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_breaker_retest":
            if prev2 is None:
                return False, False
            p2_low = float(prev2["low"])
            p2_high = float(prev2["high"])
            p2_ll20 = float(prev2["ll20"])
            p2_hh20 = float(prev2["hh20"])
            # A failed break becomes the reference level. Requiring one full
            # reclaim candle before entry avoids trading the first wick alone.
            setup_buy = (
                p2_low < p2_ll20 and prev_close > p2_ll20
                and low <= p2_ll20 + atr1 * 0.16 and close > p2_ll20
                and close > open_ and lower_wick >= max(body * 0.45, atr1 * 0.12)
                and close_location >= 0.62 and m5_close >= m5_ema20
                and 40.0 <= rsi <= 60.0 and volume_ratio >= 0.72
                and recent_move <= atr1 * 1.25
            )
            setup_sell = (
                p2_high > p2_hh20 and prev_close < p2_hh20
                and high >= p2_hh20 - atr1 * 0.16 and close < p2_hh20
                and close < open_ and upper_wick >= max(body * 0.45, atr1 * 0.12)
                and close_location <= 0.38 and m5_close <= m5_ema20
                and 40.0 <= rsi <= 60.0 and volume_ratio >= 0.72
                and recent_move >= -(atr1 * 1.25)
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_discount_reclaim":
            dealing_low = float(last1["ll20"])
            dealing_high = float(last1["hh20"])
            dealing_range = max(atr1, dealing_high - dealing_low)
            discount_edge = dealing_low + dealing_range * 0.35
            premium_edge = dealing_high - dealing_range * 0.35
            setup_buy = (
                m15_up and m5_up and m5_adx >= 14.0
                and low <= discount_edge and close > discount_edge
                and close > open_ and lower_wick >= max(body * 0.35, atr1 * 0.10)
                and close_location >= 0.62 and 38.0 <= rsi <= 58.0
                and volume_ratio >= 0.72 and recent_move <= atr1 * 1.35
            )
            setup_sell = (
                m15_down and m5_down and m5_adx >= 14.0
                and high >= premium_edge and close < premium_edge
                and close < open_ and upper_wick >= max(body * 0.35, atr1 * 0.10)
                and close_location <= 0.38 and 42.0 <= rsi <= 62.0
                and volume_ratio >= 0.72 and recent_move >= -(atr1 * 1.35)
            )
            return setup_buy, setup_sell

        if smc_mode == "smc_displacement":
            setup_buy = (
                m15_up and m5_up and m5_adx >= 19.0
                and close > float(prev1["high"]) and close > open_
                and atr1 * 0.58 <= body <= atr1 * 1.35
                and close_location >= 0.80 and volume_ratio >= 1.10
                and 54.0 <= rsi <= 72.0 and recent_move <= atr1 * 1.9
            )
            setup_sell = (
                m15_down and m5_down and m5_adx >= 19.0
                and close < float(prev1["low"]) and close < open_
                and atr1 * 0.58 <= body <= atr1 * 1.35
                and close_location <= 0.20 and volume_ratio >= 1.10
                and 28.0 <= rsi <= 46.0 and recent_move >= -(atr1 * 1.9)
            )
            return setup_buy, setup_sell

        return False, False

    accuracy_modes = tuple(ACCURACY_SETUP_TAGS)
    if mode in accuracy_modes:
        buy, sell = accuracy_setup(mode)
        return buy, sell, {
            "extra_setup": mode,
            "setup_tag": ACCURACY_SETUP_TAGS[mode],
            "close_location": round(close_location, 4),
            "volume_ratio": round(volume_ratio, 4),
            "m5_adx": round(float(last5["adx14"]), 4),
        }
    if mode == "accuracy_ensemble":
        # One dispatcher prevents several strategies from duplicating the same exposure.
        for accuracy_mode in (
            "accuracy_liquidity_sweep",
            "accuracy_range_reclaim",
            "accuracy_trend_reclaim",
            "accuracy_momentum_confirm",
        ):
            candidate_buy, candidate_sell = accuracy_setup(accuracy_mode)
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, {
                    "extra_setup": mode,
                    "selected_setup": accuracy_mode,
                    "setup_tag": ACCURACY_SETUP_TAGS[accuracy_mode],
                    "close_location": round(close_location, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "m5_adx": round(float(last5["adx14"]), 4),
                }
        return False, False, {"extra_setup": mode, "setup_tag": "HA-NONE"}

    if mode == "accuracy_selective_ensemble":
        # Keep only the two setup families that stayed profitable in the
        # walk-forward audit. One dispatcher also prevents duplicate exposure.
        for accuracy_mode in (
            "accuracy_trend_reclaim",
            "accuracy_momentum_confirm",
        ):
            candidate_buy, candidate_sell = accuracy_setup(accuracy_mode)
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, {
                    "extra_setup": mode,
                    "selected_setup": accuracy_mode,
                    "setup_tag": ACCURACY_SETUP_TAGS[accuracy_mode],
                    "close_location": round(close_location, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "m5_adx": round(float(last5["adx14"]), 4),
                }
        return False, False, {"extra_setup": mode, "setup_tag": "SC-NONE"}

    if mode == "accuracy_multi_trigger":
        # Priority keeps the narrowest patterns first and emits only one setup
        # when several trigger families recognize the same candle.
        for accuracy_mode in (
            "accuracy_two_bar_continue",
            "accuracy_adx_breakout",
        ):
            candidate_buy, candidate_sell = accuracy_setup(accuracy_mode)
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, {
                    "extra_setup": mode,
                    "selected_setup": accuracy_mode,
                    "setup_tag": ACCURACY_SETUP_TAGS[accuracy_mode],
                    "close_location": round(close_location, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "m5_adx": round(float(last5["adx14"]), 4),
                }
        return False, False, {"extra_setup": mode, "setup_tag": "SC-NONE"}

    smc_modes = (
        "smc_liquidity_sweep",
        "smc_bos_retest",
        "smc_fvg_retest",
        "smc_order_block",
        "smc_mss_retest",
        "smc_breaker_retest",
        "smc_discount_reclaim",
        "smc_displacement",
    )
    if mode in smc_modes:
        buy, sell = smc_setup(mode)
        return buy, sell, {
            "extra_setup": mode,
            "setup_tag": SMC_SETUP_TAGS[mode],
            "close_location": round(close_location, 4),
            "volume_ratio": round(volume_ratio, 4),
            "m5_adx": round(float(last5["adx14"]), 4),
        }

    if mode == "smc_ensemble":
        for smc_mode in smc_modes:
            candidate_buy, candidate_sell = smc_setup(smc_mode)
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, {
                    "extra_setup": mode,
                    "selected_setup": smc_mode,
                    "setup_tag": SMC_SETUP_TAGS[smc_mode],
                    "close_location": round(close_location, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "m5_adx": round(float(last5["adx14"]), 4),
                }
        return False, False, {"extra_setup": mode, "setup_tag": "SMC-NONE"}

    def selective_smc_candidate() -> tuple[bool, bool, str]:
        hour_utc = int(getattr(last1.get("time"), "hour", -1))
        for smc_mode in (
            "smc_liquidity_sweep",
            "smc_fvg_retest",
            "smc_order_block",
        ):
            candidate_buy, candidate_sell = smc_setup(smc_mode)
            if smc_mode == "smc_liquidity_sweep":
                candidate_buy = candidate_buy and hour_utc == 5
                candidate_sell = candidate_sell and hour_utc == 11
            elif smc_mode == "smc_fvg_retest":
                candidate_buy = False
                candidate_sell = candidate_sell and hour_utc == 7
            elif smc_mode == "smc_order_block":
                candidate_buy = candidate_buy and hour_utc == 5
                candidate_sell = False
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, smc_mode
        return False, False, ""

    if mode == "smc_selective_ensemble":
        buy, sell, selected = selective_smc_candidate()
        return buy, sell, {
            "extra_setup": mode,
            "selected_setup": selected,
            "setup_tag": SMC_SETUP_TAGS.get(selected, "SMC-NONE"),
            "close_location": round(close_location, 4),
            "volume_ratio": round(volume_ratio, 4),
            "m5_adx": round(float(last5["adx14"]), 4),
        }

    if mode == "smc_adaptive_ensemble":
        # Contextual priority: confirmed structure shifts first, then retests,
        # with displacement used only when no mean-reversion pattern is active.
        for smc_mode in (
            "smc_mss_retest",
            "smc_breaker_retest",
            "smc_fvg_retest",
            "smc_order_block",
            "smc_discount_reclaim",
            "smc_displacement",
        ):
            candidate_buy, candidate_sell = smc_setup(smc_mode)
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, {
                    "extra_setup": mode,
                    "selected_setup": smc_mode,
                    "setup_tag": SMC_SETUP_TAGS[smc_mode],
                    "close_location": round(close_location, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "m5_adx": round(float(last5["adx14"]), 4),
                }
        return False, False, {"extra_setup": mode, "setup_tag": "SMC-NONE"}

    if mode in {"gold_multi_strategy", "gold_multi_strategy_v2"}:
        for accuracy_mode in ("accuracy_two_bar_continue", "accuracy_adx_breakout"):
            candidate_buy, candidate_sell = accuracy_setup(accuracy_mode)
            if candidate_buy or candidate_sell:
                return candidate_buy, candidate_sell, {
                    "extra_setup": mode,
                    "selected_setup": accuracy_mode,
                    "setup_tag": ACCURACY_SETUP_TAGS[accuracy_mode],
                    "close_location": round(close_location, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "m5_adx": round(float(last5["adx14"]), 4),
                }
        if mode == "gold_multi_strategy_v2":
            for smc_mode in ("smc_mss_retest",):
                candidate_buy, candidate_sell = smc_setup(smc_mode)
                if candidate_buy or candidate_sell:
                    return candidate_buy, candidate_sell, {
                        "extra_setup": mode,
                        "selected_setup": smc_mode,
                        "setup_tag": SMC_SETUP_TAGS[smc_mode],
                        "close_location": round(close_location, 4),
                        "volume_ratio": round(volume_ratio, 4),
                        "m5_adx": round(float(last5["adx14"]), 4),
                    }
        buy, sell, selected = selective_smc_candidate()
        return buy, sell, {
            "extra_setup": mode,
            "selected_setup": selected,
            "setup_tag": SMC_SETUP_TAGS.get(selected, "SMC-NONE"),
            "close_location": round(close_location, 4),
            "volume_ratio": round(volume_ratio, 4),
            "m5_adx": round(float(last5["adx14"]), 4),
        }

    if mode == "ema_cross_fast":
        buy = m5_up and prev_close <= prev_ema1 and close > ema1 and close > open_ and rsi >= 49.0
        sell = m5_down and prev_close >= prev_ema1 and close < ema1 and close < open_ and rsi <= 51.0
    elif mode == "trend_continuation_fast":
        buy = m15_up and m5_up and close > ema1 and prev_close > prev_ema1 and close > open_ and body >= atr1 * 0.18 and recent_move <= atr1 * 1.8
        sell = m15_down and m5_down and close < ema1 and prev_close < prev_ema1 and close < open_ and body >= atr1 * 0.18 and recent_move >= -(atr1 * 1.8)
    elif mode == "momentum_impulse":
        buy = m5_up and close > open_ and body >= atr1 * 0.48 and close_location >= 0.68 and volume_ratio >= 0.9
        sell = m5_down and close < open_ and body >= atr1 * 0.48 and close_location <= 0.32 and volume_ratio >= 0.9
    elif mode == "bollinger_reclaim":
        buy = low <= float(last1["bb_lower"]) and close > float(last1["bb_lower"]) and close > open_ and rsi <= 46.0
        sell = high >= float(last1["bb_upper"]) and close < float(last1["bb_upper"]) and close < open_ and rsi >= 54.0
    elif mode == "liquidity_sweep":
        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)
        buy = low < float(last1["ll20"]) and close > float(last1["ll20"]) and close > open_ and lower_wick >= body * 0.5
        sell = high > float(last1["hh20"]) and close < float(last1["hh20"]) and close < open_ and upper_wick >= body * 0.5
    elif mode == "one_bar_breakout":
        buy = m5_up and close > float(prev1["high"]) and close > open_ and body >= atr1 * 0.22
        sell = m5_down and close < float(prev1["low"]) and close < open_ and body >= atr1 * 0.22
    elif mode == "rsi_reversal":
        buy = prev_rsi <= 38.0 and rsi > 38.0 and close > open_ and float(last5["close"]) >= float(last5["ema50"])
        sell = prev_rsi >= 62.0 and rsi < 62.0 and close < open_ and float(last5["close"]) <= float(last5["ema50"])
    elif mode == "volatility_expansion":
        buy = m15_up and close > open_ and body >= atr1 * 0.75 and close_location >= 0.75 and volume_ratio >= 1.0
        sell = m15_down and close < open_ and body >= atr1 * 0.75 and close_location <= 0.25 and volume_ratio >= 1.0
    elif mode == "micro_ema_bounce":
        buy = m5_up and low <= ema1 and close > ema1 and close > open_ and body >= atr1 * 0.1
        sell = m5_down and high >= ema1 and close < ema1 and close < open_ and body >= atr1 * 0.1
    elif mode == "micro_mean_revert":
        buy = prev_close < prev_ema1 - atr1 * 0.7 and close > open_ and close > prev_close and rsi <= 48.0
        sell = prev_close > prev_ema1 + atr1 * 0.7 and close < open_ and close < prev_close and rsi >= 52.0

    return buy, sell, {
        "extra_setup": mode,
        "close_location": round(close_location, 4),
        "volume_ratio": round(volume_ratio, 4),
        "m5_up": m5_up,
        "m5_down": m5_down,
        "m15_up": m15_up,
        "m15_down": m15_down,
    }
