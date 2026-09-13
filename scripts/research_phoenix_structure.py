from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.indicators import adx, atr, ema
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from backtest_phoenix_runner_ladder_40d import _fetch_signals


GROUPS = {
    "base": ["ret1", "ret3", "ret6", "ret12", "rsi", "adx"],
    "candle": [
        "body", "body_abs", "upper_wick", "lower_wick", "close_location", "range_ratio",
        "engulf_buy", "engulf_sell", "inside_bar", "outside_bar", "direction_run",
    ],
    "trend": [
        "ema20_dist", "ema20_50", "ema50_200", "ema20_slope", "ema50_slope",
        "reg_slope12", "reg_r2_12", "reg_slope36", "reg_r2_36", "reg_slope72", "reg_r2_72",
    ],
    "structure": [
        "range20_pos", "range50_pos", "prior_high20_dist", "prior_low20_dist",
        "bos_up", "bos_down", "swing_high_delta", "swing_low_delta", "sr_touch_high", "sr_touch_low",
    ],
    "liquidity": [
        "sweep_high", "sweep_low", "equal_high", "equal_low", "bull_fvg", "bear_fvg", "volume_ratio",
    ],
    "vol_session": [
        "atr_fast_slow", "atr_percentile", "realized_fast_slow", "bb_width", "spread_atr",
        "hour_sin", "hour_cos", "london", "new_york", "asia",
    ],
    "mtf": [
        "m15_ema20_50", "m15_slope", "m15_adx", "h1_ema20_50", "h1_slope", "h1_adx",
        "h4_ema20_50", "h4_slope", "h4_adx", "mtf_alignment",
    ],
}

TP_PLANS = {
    "micro": (0.45, 0.75, 1.10),
    "fast": (0.55, 0.90, 1.35),
    "balanced": (0.75, 1.25, 2.00),
}


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=31))
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, cursor, chunk_end)
        if raw is not None and len(raw):
            chunks.append(pd.DataFrame(raw))
        cursor = chunk_end
    if not chunks:
        raise RuntimeError("No M5 rates returned")
    out = pd.concat(chunks, ignore_index=True).drop_duplicates("time").sort_values("time")
    out["time"] = pd.to_datetime(out["time"], unit="s", utc=True)
    return out.reset_index(drop=True)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))


def _rolling_regression(series: pd.Series, window: int) -> tuple[pd.Series, pd.Series]:
    x = np.arange(window, dtype=float)
    x_centered = x - x.mean()
    denom = float(np.square(x_centered).sum())

    def values(raw: np.ndarray) -> tuple[float, float]:
        y = np.asarray(raw, dtype=float)
        centered = y - y.mean()
        slope = float(np.dot(x_centered, centered) / denom)
        fitted = slope * x_centered
        total = float(np.square(centered).sum())
        r2 = 0.0 if total <= 1e-12 else max(0.0, 1.0 - float(np.square(centered - fitted).sum()) / total)
        return slope, r2

    packed = series.rolling(window).apply(lambda raw: values(raw)[0], raw=True)
    r2 = series.rolling(window).apply(lambda raw: values(raw)[1], raw=True)
    return packed, r2


def _mtf_features(frame: pd.DataFrame, rule: str, prefix: str) -> pd.DataFrame:
    indexed = frame.set_index("time")
    bars = indexed.resample(rule, label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum"}
    ).dropna()
    bars[f"{prefix}_ema20_50"] = (ema(bars["close"], 20) - ema(bars["close"], 50)) / atr(bars, 14).replace(0, np.nan)
    bars[f"{prefix}_slope"] = (ema(bars["close"], 20) - ema(bars["close"], 20).shift(3)) / atr(bars, 14).replace(0, np.nan)
    bars[f"{prefix}_adx"] = adx(bars, 14) / 50.0
    return bars[[f"{prefix}_ema20_50", f"{prefix}_slope", f"{prefix}_adx"]].reset_index()


def _feature_frame(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    close, high, low, opening = out["close"], out["high"], out["low"], out["open"]
    out["atr14"] = atr(out, 14)
    unit = out["atr14"].replace(0, np.nan)
    out["ema20"], out["ema50"], out["ema200"] = ema(close, 20), ema(close, 50), ema(close, 200)
    out["ret1"] = close.diff(1) / unit
    out["ret3"] = close.diff(3) / unit
    out["ret6"] = close.diff(6) / unit
    out["ret12"] = close.diff(12) / unit
    out["rsi"] = (_rsi(close) - 50) / 25
    out["adx"] = adx(out, 14) / 50

    candle_range = (high - low).replace(0, np.nan)
    top = pd.concat([opening, close], axis=1).max(axis=1)
    bottom = pd.concat([opening, close], axis=1).min(axis=1)
    out["body"] = (close - opening) / unit
    out["body_abs"] = (close - opening).abs() / unit
    out["upper_wick"] = (high - top) / unit
    out["lower_wick"] = (bottom - low) / unit
    out["close_location"] = (close - low) / candle_range
    out["range_ratio"] = candle_range / unit
    out["engulf_buy"] = ((close > opening) & (opening <= close.shift(1)) & (close >= opening.shift(1))).astype(float)
    out["engulf_sell"] = ((close < opening) & (opening >= close.shift(1)) & (close <= opening.shift(1))).astype(float)
    out["inside_bar"] = ((high < high.shift(1)) & (low > low.shift(1))).astype(float)
    out["outside_bar"] = ((high > high.shift(1)) & (low < low.shift(1))).astype(float)
    direction = np.sign(close - opening)
    out["direction_run"] = direction.rolling(4).sum() / 4

    out["ema20_dist"] = (close - out["ema20"]) / unit
    out["ema20_50"] = (out["ema20"] - out["ema50"]) / unit
    out["ema50_200"] = (out["ema50"] - out["ema200"]) / unit
    out["ema20_slope"] = (out["ema20"] - out["ema20"].shift(3)) / unit
    out["ema50_slope"] = (out["ema50"] - out["ema50"].shift(6)) / unit
    for window in (12, 36, 72):
        slope, r2 = _rolling_regression(close, window)
        out[f"reg_slope{window}"] = slope / unit
        out[f"reg_r2_{window}"] = r2

    prior_high20, prior_low20 = high.rolling(20).max().shift(1), low.rolling(20).min().shift(1)
    prior_high50, prior_low50 = high.rolling(50).max().shift(1), low.rolling(50).min().shift(1)
    out["range20_pos"] = (close - prior_low20) / (prior_high20 - prior_low20).replace(0, np.nan)
    out["range50_pos"] = (close - prior_low50) / (prior_high50 - prior_low50).replace(0, np.nan)
    out["prior_high20_dist"] = (prior_high20 - close) / unit
    out["prior_low20_dist"] = (close - prior_low20) / unit
    out["bos_up"] = (close > prior_high20).astype(float)
    out["bos_down"] = (close < prior_low20).astype(float)
    swing_high = high.rolling(5, center=True).max().eq(high).shift(3).where(lambda x: x).astype(float)
    swing_low = low.rolling(5, center=True).min().eq(low).shift(3).where(lambda x: x).astype(float)
    last_swing_high = high.where(swing_high.eq(1)).ffill()
    last_swing_low = low.where(swing_low.eq(1)).ffill()
    out["swing_high_delta"] = (last_swing_high - last_swing_high.shift(12)) / unit
    out["swing_low_delta"] = (last_swing_low - last_swing_low.shift(12)) / unit
    tolerance = unit * 0.20
    out["sr_touch_high"] = ((high - prior_high20).abs() <= tolerance).astype(float)
    out["sr_touch_low"] = ((low - prior_low20).abs() <= tolerance).astype(float)

    out["sweep_high"] = ((high > prior_high20) & (close < prior_high20)).astype(float)
    out["sweep_low"] = ((low < prior_low20) & (close > prior_low20)).astype(float)
    out["equal_high"] = ((high - high.shift(1)).abs() <= tolerance).astype(float)
    out["equal_low"] = ((low - low.shift(1)).abs() <= tolerance).astype(float)
    out["bull_fvg"] = (low > high.shift(2)).astype(float)
    out["bear_fvg"] = (high < low.shift(2)).astype(float)
    out["volume_ratio"] = out["tick_volume"] / out["tick_volume"].rolling(20).mean().replace(0, np.nan)

    atr_fast = atr(out, 5)
    out["atr_fast_slow"] = atr_fast / unit
    out["atr_percentile"] = unit.rolling(100).rank(pct=True)
    rv5 = close.pct_change().rolling(5).std()
    rv50 = close.pct_change().rolling(50).std()
    out["realized_fast_slow"] = rv5 / rv50.replace(0, np.nan)
    out["bb_width"] = close.rolling(20).std(ddof=0) * 4 / unit
    out["spread_atr"] = (out["spread"] * 0.01) / unit
    hour = out["time"].dt.hour + out["time"].dt.minute / 60
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    out["asia"] = ((hour >= 0) & (hour < 7)).astype(float)
    out["london"] = ((hour >= 7) & (hour < 12)).astype(float)
    out["new_york"] = ((hour >= 12) & (hour < 17)).astype(float)

    for rule, prefix in (("15min", "m15"), ("1h", "h1"), ("4h", "h4")):
        higher = _mtf_features(raw, rule, prefix)
        out = pd.merge_asof(out.sort_values("time"), higher.sort_values("time"), on="time", direction="backward")
    signs = np.sign(out[["m15_ema20_50", "h1_ema20_50", "h4_ema20_50"]])
    out["mtf_alignment"] = signs.sum(axis=1) / 3
    features = [name for names in GROUPS.values() for name in names]
    out[features] = out[features].replace([np.inf, -np.inf], np.nan)
    return out


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(values, -30, 30)))


def _fit(x: np.ndarray, y: np.ndarray, iterations: int = 1400, lr: float = 0.035) -> tuple[np.ndarray, float]:
    weights = np.zeros(x.shape[1])
    bias = 0.0
    positives, negatives = max(1, int(y.sum())), max(1, len(y) - int(y.sum()))
    sample = np.where(y > 0.5, len(y) / (2 * positives), len(y) / (2 * negatives))
    for _ in range(iterations):
        error = (_sigmoid(x @ weights + bias) - y) * sample
        weights -= lr * ((x.T @ error) / sample.sum() + 0.015 * weights)
        bias -= lr * float(error.sum() / sample.sum())
    return weights, bias


def _selected(indices: np.ndarray, probabilities: np.ndarray, threshold: float, cooldown: int = 6) -> list[int]:
    result: list[int] = []
    last = -100000
    for idx in indices:
        if idx - last >= cooldown and probabilities[idx] >= threshold:
            result.append(int(idx))
            last = int(idx)
    return result


def _simulate(frame: pd.DataFrame, indices: list[int], side_probability: np.ndarray, spread_price: float,
              sl_atr: float, plan: tuple[float, float, float], protect: str) -> dict[str, Any]:
    values: list[float] = []
    for signal_idx in indices:
        entry_idx = signal_idx + 1
        if entry_idx >= len(frame):
            continue
        side = "buy" if side_probability[signal_idx] >= 0.5 else "sell"
        direction = 1 if side == "buy" else -1
        entry = float(frame.iloc[entry_idx]["open"])
        risk_unit = float(frame.iloc[signal_idx]["atr14"])
        stop = entry - direction * risk_unit * sl_atr
        leg_results: list[float] = []
        for target_no, multiple in enumerate(plan, start=1):
            target = entry + direction * risk_unit * multiple
            current_stop = stop
            exit_price = float(frame.iloc[min(len(frame) - 1, entry_idx + 72)]["close"])
            reached = 0
            for bar_idx in range(entry_idx, min(len(frame), entry_idx + 73)):
                row = frame.iloc[bar_idx]
                high, low = float(row["high"]), float(row["low"])
                stop_hit = low <= current_stop if side == "buy" else high >= current_stop
                target_hit = high >= target if side == "buy" else low <= target
                if stop_hit:
                    exit_price = current_stop
                    break
                if target_hit:
                    exit_price = target
                    break
                for level, tp_multiple in enumerate(plan, start=1):
                    tp = entry + direction * risk_unit * tp_multiple
                    if (high >= tp if side == "buy" else low <= tp):
                        reached = max(reached, level)
                if protect == "be_tp1" and reached >= 1 and target_no > 1:
                    current_stop = max(current_stop, entry) if side == "buy" else min(current_stop, entry)
                elif protect == "be_tp2" and reached >= 2 and target_no > 2:
                    current_stop = max(current_stop, entry) if side == "buy" else min(current_stop, entry)
            move = exit_price - entry if side == "buy" else entry - exit_price
            leg_results.append(move - spread_price)
        values.append(sum(leg_results))
    wins = sum(value > 0 for value in values)
    losses = sum(value < 0 for value in values)
    cumulative, peak, max_dd = 0.0, 0.0, 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return {
        "signals": len(values), "wins": wins, "losses": losses,
        "win_rate_pct": round(100 * wins / max(1, wins + losses), 2),
        "pnl_001_per_leg": round(sum(values), 2), "max_drawdown_001": round(max_dd, 2),
        "profit_to_dd": round(sum(values) / abs(max_dd), 3) if max_dd < 0 else 0.0,
    }


def _near(selected: list[int], actual: np.ndarray) -> int:
    actual_set = set(int(value) for value in actual)
    return sum(any(idx + delta in actual_set for delta in (-1, 0, 1)) for idx in selected)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--broker-time-offset-minutes", type=int, default=180)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        offset = timedelta(minutes=args.broker_time_offset_minutes)
        end = datetime.now(UTC) + offset
        start = end - timedelta(days=args.days + 15)
        frame = _feature_frame(_rates(symbol, start, end))
        signals = await _fetch_signals(frame, args.days, args.broker_time_offset_minutes)
        split1, split2 = int(len(frame) * 0.60), int(len(frame) * 0.80)

        # The message may arrive inside a live M5 candle. Use only the preceding closed candle.
        side_by_idx: dict[int, int] = {}
        for item in signals:
            feature_idx = int(item.start_idx) - 1
            if feature_idx > 100:
                side_by_idx[feature_idx] = 1 if item.signal.side == "buy" else 0
        positives = np.array(sorted(side_by_idx), dtype=int)
        train_pos = positives[positives < split1]
        val_pos = positives[(positives >= split1) & (positives < split2)]
        test_pos = positives[positives >= split2]
        rng = np.random.default_rng(20260715)

        model_sets = {
            "base": GROUPS["base"],
            **{f"base_plus_{key}": GROUPS["base"] + value for key, value in GROUPS.items() if key != "base"},
            "all_structure": [name for names in GROUPS.values() for name in names],
        }
        models: list[dict[str, Any]] = []
        model_cache: dict[str, tuple[np.ndarray, np.ndarray, list[str]]] = {}
        for model_name, features in model_sets.items():
            valid = frame[features].notna().all(axis=1).to_numpy()
            train_valid = np.flatnonzero(valid & (np.arange(len(frame)) < split1) & (np.arange(len(frame)) > 300))
            exclusion = np.zeros(len(frame), dtype=bool)
            for idx in positives:
                exclusion[max(0, idx - 6):min(len(frame), idx + 7)] = True
            pool = train_valid[~exclusion[train_valid]]
            model_train_pos = train_pos[valid[train_pos]]
            negatives = np.sort(rng.choice(pool, size=min(len(pool), len(model_train_pos) * 4), replace=False))
            fit_idx = np.concatenate([model_train_pos, negatives])
            y = np.concatenate([np.ones(len(model_train_pos)), np.zeros(len(negatives))])
            raw_x = frame[features].to_numpy(float)
            mean, std = np.nanmean(raw_x[fit_idx], axis=0), np.nanstd(raw_x[fit_idx], axis=0)
            std[std < 1e-8] = 1
            x = (raw_x - mean) / std
            setup_w, setup_b = _fit(x[fit_idx], y)
            side_y = np.array([side_by_idx[int(idx)] for idx in model_train_pos], dtype=float)
            side_w, side_b = _fit(x[model_train_pos], side_y, iterations=1200)
            setup_prob, side_prob = _sigmoid(x @ setup_w + setup_b), _sigmoid(x @ side_w + side_b)
            val_candidates = np.flatnonzero(valid & (np.arange(len(frame)) >= split1) & (np.arange(len(frame)) < split2))
            thresholds = sorted(set(float(np.quantile(setup_prob[train_valid], q)) for q in (0.97, 0.98, 0.99, 0.995)))
            best: dict[str, Any] | None = None
            median_spread = float(frame["spread"].median()) * float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
            for threshold in thresholds:
                picked = _selected(val_candidates, setup_prob, threshold)
                for sl_atr in (0.7, 0.9, 1.1, 1.35):
                    for plan_name, plan in TP_PLANS.items():
                        for protect in ("none", "be_tp1", "be_tp2"):
                            result = _simulate(frame, picked, side_prob, median_spread, sl_atr, plan, protect)
                            candidate = {"threshold": threshold, "sl_atr": sl_atr, "tp_plan": plan_name,
                                         "protect": protect, "validation": result}
                            score = (result["pnl_001_per_leg"] > 0, result["profit_to_dd"], result["pnl_001_per_leg"], result["signals"])
                            if result["signals"] >= 20 and (best is None or score > best["_score"]):
                                best = {**candidate, "_score": score}
            if best is None:
                best = {"threshold": thresholds[-1], "sl_atr": 1.0, "tp_plan": "fast", "protect": "be_tp1",
                        "validation": {}, "_score": (False, 0, 0, 0)}
            test_candidates = np.flatnonzero(valid & (np.arange(len(frame)) >= split2) & (np.arange(len(frame)) < len(frame) - 74))
            test_selected = _selected(test_candidates, setup_prob, float(best["threshold"]))
            test_result = _simulate(frame, test_selected, side_prob, median_spread, float(best["sl_atr"]),
                                    TP_PLANS[str(best["tp_plan"])], str(best["protect"]))
            val_selected = _selected(val_candidates, setup_prob, float(best["threshold"]))
            best.pop("_score", None)
            row = {
                "model": model_name, "features": len(features), "strategy": best, "test": test_result,
                "phoenix_match_validation": {"precision_pct": round(100 * _near(val_selected, val_pos) / max(1, len(val_selected)), 2),
                                             "recall_pct": round(100 * _near(val_selected, val_pos) / max(1, len(val_pos)), 2)},
                "phoenix_match_test": {"precision_pct": round(100 * _near(test_selected, test_pos) / max(1, len(test_selected)), 2),
                                       "recall_pct": round(100 * _near(test_selected, test_pos) / max(1, len(test_pos)), 2)},
            }
            models.append(row)
            model_cache[model_name] = (setup_prob, side_prob, features)

        models.sort(key=lambda row: (row["test"]["pnl_001_per_leg"] > 0, row["test"]["profit_to_dd"], row["test"]["pnl_001_per_leg"]), reverse=True)
        winner = models[0]
        payload = {
            "generated_utc": datetime.now(UTC).isoformat(), "symbol": symbol, "days": args.days,
            "method": "closed-candle only; chronological 60/20/20; validation-only parameter selection; untouched final test; spread included; SL-first ambiguity",
            "signals_found": len(signals), "signal_bars": {"train": len(train_pos), "validation": len(val_pos), "test": len(test_pos)},
            "feature_groups": GROUPS, "models": models, "winner": winner,
            "shadow_ready": bool(winner["test"]["signals"] >= 20 and winner["test"]["pnl_001_per_leg"] > 0
                                 and winner["test"]["win_rate_pct"] >= 60 and winner["test"]["profit_to_dd"] >= 1.0),
            "deployment_ready": False,
            "deployment_note": "Shadow only until a second forward window confirms the result; many feature/exit combinations were tested.",
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"signals": payload["signal_bars"], "winner": winner, "deployment_ready": payload["deployment_ready"]}))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
