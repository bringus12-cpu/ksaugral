from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    avg_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_up / avg_down.replace(0.0, float("nan"))
    result = 100.0 - (100.0 / (1.0 + rs))
    result = result.mask((avg_down == 0.0) & (avg_up > 0.0), 100.0)
    result = result.mask((avg_up == 0.0) & (avg_down > 0.0), 0.0)
    return result.mask((avg_up == 0.0) & (avg_down == 0.0), 50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_series = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_series.replace(0.0, pd.NA)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_series.replace(0.0, pd.NA)
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, pd.NA)).fillna(0.0)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean()


def bollinger_bands(series: pd.Series, period: int = 20, std_mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    basis = series.rolling(period).mean()
    dev = series.rolling(period).std(ddof=0)
    upper = basis + dev * std_mult
    lower = basis - dev * std_mult
    return basis, upper, lower


def macd(
    series: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_line = ema(series, fast_period)
    slow_line = ema(series, slow_period)
    main_line = fast_line - slow_line
    signal_line = ema(main_line, signal_period)
    return main_line, signal_line, main_line - signal_line


def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1.0, period + 1.0)
    denominator = float(weights.sum())
    return series.rolling(period).apply(lambda values: float(np.dot(values, weights) / denominator), raw=True)


def dema(series: pd.Series, period: int) -> pd.Series:
    first = ema(series, period)
    return 2.0 * first - ema(first, period)


def tema(series: pd.Series, period: int) -> pd.Series:
    first = ema(series, period)
    second = ema(first, period)
    third = ema(second, period)
    return 3.0 * first - 3.0 * second + third


def hma(series: pd.Series, period: int) -> pd.Series:
    half = max(2, period // 2)
    root = max(2, int(period**0.5))
    return wma(2.0 * wma(series, half) - wma(series, period), root)


def kama(series: pd.Series, period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    change = series.diff(period).abs()
    volatility = series.diff().abs().rolling(period).sum()
    efficiency = (change / volatility.replace(0.0, np.nan)).fillna(0.0)
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    smoothing = (efficiency * (fast_sc - slow_sc) + slow_sc) ** 2
    output = series.astype(float).copy()
    if output.empty:
        return output
    output.iloc[0] = float(series.iloc[0])
    for index in range(1, len(output)):
        previous = float(output.iloc[index - 1])
        output.iloc[index] = previous + float(smoothing.iloc[index]) * (float(series.iloc[index]) - previous)
    return output


def money_flow_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    raw_flow = typical * df["tick_volume"].clip(lower=0.0)
    direction = typical.diff()
    positive = raw_flow.where(direction > 0.0, 0.0).rolling(period).sum()
    negative = raw_flow.where(direction < 0.0, 0.0).rolling(period).sum()
    ratio = positive / negative.replace(0.0, np.nan)
    result = 100.0 - 100.0 / (1.0 + ratio)
    return result.mask((negative == 0.0) & (positive > 0.0), 100.0).fillna(50.0)


def ultimate_oscillator(df: pd.DataFrame) -> pd.Series:
    prior_close = df["close"].shift(1)
    buying_pressure = df["close"] - pd.concat([df["low"], prior_close], axis=1).min(axis=1)
    true_range = pd.concat([df["high"], prior_close], axis=1).max(axis=1) - pd.concat(
        [df["low"], prior_close], axis=1
    ).min(axis=1)
    avg7 = buying_pressure.rolling(7).sum() / true_range.rolling(7).sum().replace(0.0, np.nan)
    avg14 = buying_pressure.rolling(14).sum() / true_range.rolling(14).sum().replace(0.0, np.nan)
    avg28 = buying_pressure.rolling(28).sum() / true_range.rolling(28).sum().replace(0.0, np.nan)
    return 100.0 * (4.0 * avg7 + 2.0 * avg14 + avg28) / 7.0


def aroon(df: pd.DataFrame, period: int = 25) -> tuple[pd.Series, pd.Series]:
    up = df["high"].rolling(period + 1).apply(lambda values: 100.0 * np.argmax(values) / period, raw=True)
    down = df["low"].rolling(period + 1).apply(lambda values: 100.0 * np.argmin(values) / period, raw=True)
    return up, down


def supertrend(df: pd.DataFrame, atr_series: pd.Series, multiplier: float = 3.0) -> tuple[pd.Series, pd.Series]:
    midpoint = (df["high"] + df["low"]) / 2.0
    basic_upper = midpoint + multiplier * atr_series
    basic_lower = midpoint - multiplier * atr_series
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    trend_line = midpoint.copy()
    direction = pd.Series(1.0, index=df.index)
    for index in range(1, len(df)):
        prior_close = float(df["close"].iloc[index - 1])
        final_upper.iloc[index] = (
            float(basic_upper.iloc[index])
            if float(basic_upper.iloc[index]) < float(final_upper.iloc[index - 1])
            or prior_close > float(final_upper.iloc[index - 1])
            else float(final_upper.iloc[index - 1])
        )
        final_lower.iloc[index] = (
            float(basic_lower.iloc[index])
            if float(basic_lower.iloc[index]) > float(final_lower.iloc[index - 1])
            or prior_close < float(final_lower.iloc[index - 1])
            else float(final_lower.iloc[index - 1])
        )
        close = float(df["close"].iloc[index])
        previous_direction = float(direction.iloc[index - 1])
        if previous_direction < 0.0 and close > float(final_upper.iloc[index]):
            direction.iloc[index] = 1.0
        elif previous_direction > 0.0 and close < float(final_lower.iloc[index]):
            direction.iloc[index] = -1.0
        else:
            direction.iloc[index] = previous_direction
        trend_line.iloc[index] = final_lower.iloc[index] if direction.iloc[index] > 0.0 else final_upper.iloc[index]
    return trend_line, direction


def rolling_slope(series: pd.Series, period: int = 20) -> pd.Series:
    x = np.arange(period, dtype=float)
    denominator = float(((x - x.mean()) ** 2).sum())
    return series.rolling(period).apply(
        lambda values: float(np.dot(x - x.mean(), values - values.mean()) / denominator),
        raw=True,
    )


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out, 14)
    out["adx14"] = adx(out, 14)
    out["hh20"] = out["high"].rolling(20).max().shift(1)
    out["ll20"] = out["low"].rolling(20).min().shift(1)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = bollinger_bands(out["close"], 20, 2.0)
    bb_span = (out["bb_upper"] - out["bb_lower"]).replace(0.0, float("nan"))
    out["bb_width"] = bb_span / out["bb_mid"].abs().replace(0.0, float("nan"))
    out["bb_width_median"] = out["bb_width"].rolling(100).median()
    out["bb_percent_b"] = (out["close"] - out["bb_lower"]) / bb_span
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    out["bb_fast_mid"], out["bb_fast_upper"], out["bb_fast_lower"] = bollinger_bands(out["close"], 14, 1.8)
    out["macd_fast"], out["macd_fast_signal"], out["macd_fast_hist"] = macd(out["close"], 8, 21, 5)
    out["bb_slow_mid"], out["bb_slow_upper"], out["bb_slow_lower"] = bollinger_bands(out["close"], 30, 2.2)
    slow_span = (out["bb_slow_upper"] - out["bb_slow_lower"]).replace(0.0, float("nan"))
    out["bb_slow_percent_b"] = (out["close"] - out["bb_slow_lower"]) / slow_span
    out["macd_slow"], out["macd_slow_signal"], out["macd_slow_hist"] = macd(out["close"], 16, 35, 9)
    out["keltner_fast_upper"] = out["ema20"] + out["atr14"] * 1.4
    out["keltner_fast_lower"] = out["ema20"] - out["atr14"] * 1.4
    out["bb_keltner_fast_squeeze"] = (
        (out["bb_fast_upper"] < out["keltner_fast_upper"])
        & (out["bb_fast_lower"] > out["keltner_fast_lower"])
    )
    low14 = out["low"].rolling(14).min()
    high14 = out["high"].rolling(14).max()
    stochastic_span = (high14 - low14).replace(0.0, float("nan"))
    out["stoch_k"] = 100.0 * (out["close"] - low14) / stochastic_span
    out["stoch_d"] = out["stoch_k"].rolling(3).mean()
    candle_span = (out["high"] - out["low"]).replace(0.0, float("nan"))
    out["close_location"] = (out["close"] - out["low"]) / candle_span
    out["volume_ma20"] = out["tick_volume"].rolling(20).mean()
    out["volume_ratio"] = out["tick_volume"] / out["volume_ma20"].replace(0.0, float("nan"))
    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    volume = out["tick_volume"].clip(lower=1.0)
    if "time" in out.columns:
        session_key = (pd.to_datetime(out["time"], utc=True) + pd.Timedelta(hours=2)).dt.date
        out["vwap"] = (typical * volume).groupby(session_key).cumsum() / volume.groupby(session_key).cumsum()
    else:
        out["vwap"] = (typical * volume).cumsum() / volume.cumsum()

    # Extended indicator laboratory.  Every column is calculated from current
    # and past bars only so the same frame can be used in live and backtest code.
    out["sma20"] = out["close"].rolling(20).mean()
    out["sma50"] = out["close"].rolling(50).mean()
    out["wma20"] = wma(out["close"], 20)
    out["dema20"] = dema(out["close"], 20)
    out["tema20"] = tema(out["close"], 20)
    out["hma20"] = hma(out["close"], 20)
    out["kama10"] = kama(out["close"], 10)
    out["vwma20"] = (out["close"] * volume).rolling(20).sum() / volume.rolling(20).sum().replace(0.0, np.nan)
    out["roc12"] = out["close"].pct_change(12) * 100.0
    out["momentum10"] = out["close"] - out["close"].shift(10)

    mean_dev = typical.rolling(20).apply(lambda values: float(np.mean(np.abs(values - values.mean()))), raw=True)
    out["cci20"] = (typical - typical.rolling(20).mean()) / (0.015 * mean_dev.replace(0.0, np.nan))
    high14 = out["high"].rolling(14).max()
    low14 = out["low"].rolling(14).min()
    out["williams_r14"] = -100.0 * (high14 - out["close"]) / (high14 - low14).replace(0.0, np.nan)
    out["mfi14"] = money_flow_index(out, 14)

    signed_volume = np.sign(out["close"].diff().fillna(0.0)) * volume
    out["obv"] = signed_volume.cumsum()
    money_flow_multiplier = ((out["close"] - out["low"]) - (out["high"] - out["close"])) / candle_span
    money_flow_volume = money_flow_multiplier.fillna(0.0) * volume
    out["cmf20"] = money_flow_volume.rolling(20).sum() / volume.rolling(20).sum().replace(0.0, np.nan)
    out["adl"] = money_flow_volume.cumsum()
    out["adosc"] = ema(out["adl"], 3) - ema(out["adl"], 10)

    ema12 = ema(out["close"], 12)
    ema26 = ema(out["close"], 26)
    out["ppo"] = 100.0 * (ema12 - ema26) / ema26.abs().replace(0.0, np.nan)
    out["ppo_signal"] = ema(out["ppo"], 9)
    delta = out["close"].diff()
    up_sum = delta.clip(lower=0.0).rolling(14).sum()
    down_sum = (-delta.clip(upper=0.0)).rolling(14).sum()
    out["cmo14"] = 100.0 * (up_sum - down_sum) / (up_sum + down_sum).replace(0.0, np.nan)
    triple = ema(ema(ema(out["close"], 15), 15), 15)
    out["trix15"] = triple.pct_change() * 100.0
    out["ultimate_osc"] = ultimate_oscillator(out)
    midpoint = (out["high"] + out["low"]) / 2.0
    out["awesome_osc"] = midpoint.rolling(5).mean() - midpoint.rolling(34).mean()

    out["aroon_up"], out["aroon_down"] = aroon(out, 25)
    out["aroon_osc"] = out["aroon_up"] - out["aroon_down"]
    out["dpo20"] = out["close"].shift(11) - out["sma20"]
    out["natr14"] = 100.0 * out["atr14"] / out["close"].abs().replace(0.0, np.nan)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["choppiness14"] = 100.0 * np.log10(
        true_range.rolling(14).sum() / (high14 - low14).replace(0.0, np.nan)
    ) / np.log10(14.0)
    out["efficiency10"] = out["close"].diff(10).abs() / out["close"].diff().abs().rolling(10).sum().replace(0.0, np.nan)
    out["zscore20"] = (out["close"] - out["sma20"]) / out["close"].rolling(20).std(ddof=0).replace(0.0, np.nan)
    out["linreg_slope20"] = rolling_slope(out["close"], 20)

    rolling_min = out["low"].rolling(10).min()
    rolling_max = out["high"].rolling(10).max()
    fisher_source = (2.0 * ((out["close"] - rolling_min) / (rolling_max - rolling_min).replace(0.0, np.nan)) - 1.0).clip(-0.999, 0.999)
    out["fisher10"] = 0.5 * np.log((1.0 + fisher_source) / (1.0 - fisher_source))
    out["supertrend"], out["supertrend_dir"] = supertrend(out, out["atr14"], 3.0)

    out["ichimoku_tenkan"] = (out["high"].rolling(9).max() + out["low"].rolling(9).min()) / 2.0
    out["ichimoku_kijun"] = (out["high"].rolling(26).max() + out["low"].rolling(26).min()) / 2.0
    out["ichimoku_span_a"] = (out["ichimoku_tenkan"] + out["ichimoku_kijun"]) / 2.0
    out["ichimoku_span_b"] = (out["high"].rolling(52).max() + out["low"].rolling(52).min()) / 2.0
    out["donchian_upper20"] = out["high"].rolling(20).max().shift(1)
    out["donchian_lower20"] = out["low"].rolling(20).min().shift(1)
    out["donchian_mid20"] = (out["donchian_upper20"] + out["donchian_lower20"]) / 2.0
    out["balance_of_power"] = (out["close"] - out["open"]) / candle_span
    out["force_index13"] = ema(out["close"].diff() * volume, 13)
    distance_moved = ((out["high"] + out["low"]) / 2.0).diff()
    box_ratio = volume / (out["high"] - out["low"]).replace(0.0, np.nan)
    out["ease_of_movement14"] = (distance_moved / box_ratio.replace(0.0, np.nan)).rolling(14).mean()
    high_low_ratio = out["high"] / out["low"].replace(0.0, np.nan)
    amplitude = high_low_ratio - 1.0 / high_low_ratio
    out["mass_index25"] = (ema(amplitude, 9) / ema(ema(amplitude, 9), 9).replace(0.0, np.nan)).rolling(25).sum()
    rcma1 = out["close"].pct_change(10).rolling(10).sum()
    rcma2 = out["close"].pct_change(15).rolling(10).sum() * 2.0
    rcma3 = out["close"].pct_change(20).rolling(10).sum() * 3.0
    rcma4 = out["close"].pct_change(30).rolling(15).sum() * 4.0
    out["kst"] = 100.0 * (rcma1 + rcma2 + rcma3 + rcma4)
    out["kst_signal"] = out["kst"].rolling(9).mean()
    out["realized_vol20"] = out["close"].pct_change().rolling(20).std(ddof=0) * np.sqrt(20.0)
    return out
