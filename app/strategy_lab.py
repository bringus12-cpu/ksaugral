from __future__ import annotations

import math

import pandas as pd


LAB_STRATEGY_DESCRIPTIONS = {
    "cci_reversal_specialist": "Powrot CCI z ekstremum z potwierdzeniem kierunku swiecy.",
    "williams_reversal_specialist": "Powrot Williams %R ze strefy wyprzedania lub wykupienia.",
    "stochastic_cross_specialist": "Przeciecie Stochastic K/D w strefie skrajnej.",
    "mfi_reversal_specialist": "Odwrocenie Money Flow Index potwierdzone przeplywem wolumenu.",
    "supertrend_specialist": "Kontynuacja po zmianie kierunku Supertrend zgodnej z EMA M5.",
    "ichimoku_specialist": "Zgodnosc ceny, Tenkan/Kijun i chmury Ichimoku.",
    "aroon_specialist": "Trend potwierdzony przewaga Aroon Up lub Down.",
    "kst_specialist": "Przeciecie Know Sure Thing zgodne z rezimem M15.",
    "ppo_specialist": "Przeciecie Percentage Price Oscillator zgodne z trendem.",
    "roc_acceleration_specialist": "Przyspieszenie ROC w kierunku wyzszego interwalu.",
    "cmf_flow_specialist": "Trend wsparty dodatnim lub ujemnym Chaikin Money Flow.",
    "obv_confirmation_specialist": "Kierunek ceny potwierdzony nachyleniem On Balance Volume.",
    "fisher_reversal_specialist": "Powrot Fisher Transform z ekstremum cenowego.",
    "linreg_pullback_specialist": "Powrot do EMA przy stabilnym nachyleniu regresji liniowej.",
    "efficiency_trend_specialist": "Trend tylko przy wysokiej efektywnosci ruchu i niskiej chaotycznosci.",
    "hma_dema_cross_specialist": "Szybkie przeciecie Hull MA i DEMA z filtrem wyzszego trendu.",
    "ultimate_reversal_specialist": "Powrot Ultimate Oscillator z poziomu 30 albo 70.",
    "cmo_momentum_specialist": "Impuls Chande Momentum Oscillator zgodny z trendem.",
    "force_index_specialist": "Zmiana znaku Elder Force Index zgodna z EMA M5.",
    "adosc_flow_specialist": "Zmiana znaku Chaikin A/D Oscillator potwierdzajaca przeplyw kapitalu.",
}


LAB_STRATEGY_WEIGHTS = {name: 1.0 for name in LAB_STRATEGY_DESCRIPTIONS}


def _number(row: pd.Series, name: str, default: float = 0.0) -> float:
    try:
        value = float(row[name])
    except Exception:
        return default
    return value if math.isfinite(value) else default


def _cross_up(previous: pd.Series, current: pd.Series, left: str, right: str) -> bool:
    return _number(previous, left) <= _number(previous, right) and _number(current, left) > _number(current, right)


def _cross_down(previous: pd.Series, current: pd.Series, left: str, right: str) -> bool:
    return _number(previous, left) >= _number(previous, right) and _number(current, left) < _number(current, right)


def _level_cross_up(previous: pd.Series, current: pd.Series, column: str, level: float) -> bool:
    return _number(previous, column) <= level < _number(current, column)


def _level_cross_down(previous: pd.Series, current: pd.Series, column: str, level: float) -> bool:
    return _number(previous, column) >= level > _number(current, column)


def indicator_strategy_votes(m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, spread: float) -> list[tuple[str, str, float, str]]:
    one, previous = m1.iloc[-2], m1.iloc[-3]
    five, previous_five = m5.iloc[-2], m5.iloc[-3]
    fifteen = m15.iloc[-2]
    atr = max(_number(one, "atr14", 0.00001), 0.00001)
    close = _number(one, "close")
    trend_buy = _number(five, "ema20") > _number(five, "ema50") and _number(fifteen, "ema20") > _number(fifteen, "ema50")
    trend_sell = _number(five, "ema20") < _number(five, "ema50") and _number(fifteen, "ema20") < _number(fifteen, "ema50")
    adx = _number(five, "adx14")
    spread_quality = spread > 0.0 and spread / atr <= 0.30

    def side_for(buy: bool, sell: bool) -> str:
        return "buy" if buy and not sell else "sell" if sell and not buy else "hold"

    votes: list[tuple[str, str, float, str]] = []

    cci_buy = _level_cross_up(previous, one, "cci20", -100.0) and close > _number(one, "open")
    cci_sell = _level_cross_down(previous, one, "cci20", 100.0) and close < _number(one, "open")
    votes.append(("cci_reversal_specialist", side_for(cci_buy, cci_sell), 0.62, f"CCI {_number(one, 'cci20'):.1f}"))

    will_buy = _level_cross_up(previous, one, "williams_r14", -80.0)
    will_sell = _level_cross_down(previous, one, "williams_r14", -20.0)
    votes.append(("williams_reversal_specialist", side_for(will_buy, will_sell), 0.61, f"Williams {_number(one, 'williams_r14'):.1f}"))

    stoch_buy = _cross_up(previous, one, "stoch_k", "stoch_d") and _number(previous, "stoch_k") <= 25.0
    stoch_sell = _cross_down(previous, one, "stoch_k", "stoch_d") and _number(previous, "stoch_k") >= 75.0
    votes.append(("stochastic_cross_specialist", side_for(stoch_buy, stoch_sell), 0.63, f"Stoch {_number(one, 'stoch_k'):.1f}/{_number(one, 'stoch_d'):.1f}"))

    mfi_buy = _level_cross_up(previous, one, "mfi14", 25.0) and _number(one, "cmf20") >= -0.05
    mfi_sell = _level_cross_down(previous, one, "mfi14", 75.0) and _number(one, "cmf20") <= 0.05
    votes.append(("mfi_reversal_specialist", side_for(mfi_buy, mfi_sell), 0.64, f"MFI {_number(one, 'mfi14'):.1f}"))

    super_buy = _number(previous, "supertrend_dir") < 0 < _number(one, "supertrend_dir") and trend_buy
    super_sell = _number(previous, "supertrend_dir") > 0 > _number(one, "supertrend_dir") and trend_sell
    votes.append(("supertrend_specialist", side_for(super_buy, super_sell), min(0.90, 0.62 + max(0.0, adx - 18.0) / 100.0), "Supertrend flip + EMA"))

    cloud_high = max(_number(one, "ichimoku_span_a"), _number(one, "ichimoku_span_b"))
    cloud_low = min(_number(one, "ichimoku_span_a"), _number(one, "ichimoku_span_b"))
    ichi_buy = close > cloud_high and _number(one, "ichimoku_tenkan") > _number(one, "ichimoku_kijun") and trend_buy
    ichi_sell = close < cloud_low and _number(one, "ichimoku_tenkan") < _number(one, "ichimoku_kijun") and trend_sell
    votes.append(("ichimoku_specialist", side_for(ichi_buy, ichi_sell), 0.68, "Cena i Tenkan/Kijun poza chmura"))

    aroon_buy = _number(one, "aroon_osc") >= 55.0 and trend_buy
    aroon_sell = _number(one, "aroon_osc") <= -55.0 and trend_sell
    votes.append(("aroon_specialist", side_for(aroon_buy, aroon_sell), 0.64, f"Aroon osc {_number(one, 'aroon_osc'):.1f}"))

    kst_buy = _cross_up(previous, one, "kst", "kst_signal") and trend_buy
    kst_sell = _cross_down(previous, one, "kst", "kst_signal") and trend_sell
    votes.append(("kst_specialist", side_for(kst_buy, kst_sell), 0.64, "KST cross + trend"))

    ppo_buy = _cross_up(previous, one, "ppo", "ppo_signal") and trend_buy
    ppo_sell = _cross_down(previous, one, "ppo", "ppo_signal") and trend_sell
    votes.append(("ppo_specialist", side_for(ppo_buy, ppo_sell), 0.65, f"PPO {_number(one, 'ppo'):.3f}"))

    roc_buy = _number(one, "roc12") > 0.0 and _number(one, "roc12") > _number(previous, "roc12") and trend_buy and adx >= 17.0
    roc_sell = _number(one, "roc12") < 0.0 and _number(one, "roc12") < _number(previous, "roc12") and trend_sell and adx >= 17.0
    votes.append(("roc_acceleration_specialist", side_for(roc_buy, roc_sell), 0.63, f"ROC {_number(one, 'roc12'):.2f}"))

    cmf_buy = _number(one, "cmf20") >= 0.10 and trend_buy
    cmf_sell = _number(one, "cmf20") <= -0.10 and trend_sell
    votes.append(("cmf_flow_specialist", side_for(cmf_buy, cmf_sell), 0.65, f"CMF {_number(one, 'cmf20'):.2f}"))

    obv_delta = _number(one, "obv") - _number(m1.iloc[-7], "obv")
    obv_buy = obv_delta > 0.0 and trend_buy
    obv_sell = obv_delta < 0.0 and trend_sell
    votes.append(("obv_confirmation_specialist", side_for(obv_buy, obv_sell), 0.61, f"OBV delta {obv_delta:.0f}"))

    fisher_buy = _level_cross_up(previous, one, "fisher10", -1.0) and _number(one, "zscore20") < 0.0
    fisher_sell = _level_cross_down(previous, one, "fisher10", 1.0) and _number(one, "zscore20") > 0.0
    votes.append(("fisher_reversal_specialist", side_for(fisher_buy, fisher_sell), 0.63, f"Fisher {_number(one, 'fisher10'):.2f}"))

    slope = _number(five, "linreg_slope20")
    linreg_buy = slope > 0.0 and _number(one, "low") <= _number(one, "ema20") < close and trend_buy
    linreg_sell = slope < 0.0 and _number(one, "high") >= _number(one, "ema20") > close and trend_sell
    votes.append(("linreg_pullback_specialist", side_for(linreg_buy, linreg_sell), 0.65, f"Slope {slope:.5f}"))

    efficiency = _number(one, "efficiency10")
    efficient_buy = efficiency >= 0.38 and _number(one, "choppiness14") <= 55.0 and trend_buy
    efficient_sell = efficiency >= 0.38 and _number(one, "choppiness14") <= 55.0 and trend_sell
    votes.append(("efficiency_trend_specialist", side_for(efficient_buy, efficient_sell), min(0.88, 0.58 + efficiency * 0.30), f"ER {efficiency:.2f}"))

    hma_buy = _cross_up(previous, one, "hma20", "dema20") and trend_buy
    hma_sell = _cross_down(previous, one, "hma20", "dema20") and trend_sell
    votes.append(("hma_dema_cross_specialist", side_for(hma_buy, hma_sell), 0.64, "HMA/DEMA cross"))

    ultimate_buy = _level_cross_up(previous, one, "ultimate_osc", 30.0)
    ultimate_sell = _level_cross_down(previous, one, "ultimate_osc", 70.0)
    votes.append(("ultimate_reversal_specialist", side_for(ultimate_buy, ultimate_sell), 0.61, f"UO {_number(one, 'ultimate_osc'):.1f}"))

    cmo_buy = _level_cross_up(previous, one, "cmo14", 20.0) and trend_buy
    cmo_sell = _level_cross_down(previous, one, "cmo14", -20.0) and trend_sell
    votes.append(("cmo_momentum_specialist", side_for(cmo_buy, cmo_sell), 0.63, f"CMO {_number(one, 'cmo14'):.1f}"))

    force_buy = _level_cross_up(previous, one, "force_index13", 0.0) and trend_buy
    force_sell = _level_cross_down(previous, one, "force_index13", 0.0) and trend_sell
    votes.append(("force_index_specialist", side_for(force_buy, force_sell), 0.64, "Force Index zero cross"))

    adosc_buy = _level_cross_up(previous, one, "adosc", 0.0) and trend_buy
    adosc_sell = _level_cross_down(previous, one, "adosc", 0.0) and trend_sell
    votes.append(("adosc_flow_specialist", side_for(adosc_buy, adosc_sell), 0.64, "A/D oscillator zero cross"))

    if not spread_quality:
        votes = [(name, "hold", max(confidence, 0.75), f"Spread/ATR veto; {reason}") for name, _, confidence, reason in votes]
    return votes
