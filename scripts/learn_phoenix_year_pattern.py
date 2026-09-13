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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from backtest_phoenix_runner_ladder_40d import _fetch_signals, _profit


FEATURES = (
    "ret1_atr",
    "ret3_atr",
    "ret6_atr",
    "ret12_atr",
    "ret36_atr",
    "ema20_dist_atr",
    "ema50_dist_atr",
    "ema20_50_atr",
    "ema50_200_atr",
    "rsi_scaled",
    "adx_scaled",
    "bb_position",
    "range20_position",
    "body_atr",
    "upper_wick_atr",
    "lower_wick_atr",
    "volume_ratio",
    "hour_sin",
    "hour_cos",
)

TP_PLANS = {
    "fast_05_10_15": (0.5, 1.0, 1.5),
    "balanced_075_15_25": (0.75, 1.5, 2.5),
    "trend_10_20_30": (1.0, 2.0, 3.0),
}


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    chunks = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=31))
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, cursor, chunk_end)
        if raw is not None and len(raw):
            chunks.append(pd.DataFrame(raw))
        cursor = chunk_end
    if not chunks:
        raise RuntimeError("No M5 rates returned")
    frame = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["time"]).sort_values("time")
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _feature_frame(rates: pd.DataFrame) -> pd.DataFrame:
    out = rates.copy()
    atr = out["atr14"].replace(0.0, np.nan)
    close = out["close"]
    for bars in (1, 3, 6, 12, 36):
        out[f"ret{bars}_atr"] = (close - close.shift(bars)) / atr
    out["ema20_dist_atr"] = (close - out["ema20"]) / atr
    out["ema50_dist_atr"] = (close - out["ema50"]) / atr
    out["ema20_50_atr"] = (out["ema20"] - out["ema50"]) / atr
    out["ema50_200_atr"] = (out["ema50"] - out["ema200"]) / atr
    out["rsi_scaled"] = (out["rsi14"] - 50.0) / 25.0
    out["adx_scaled"] = out["adx14"] / 50.0
    bb_width = (out["bb_upper"] - out["bb_lower"]).replace(0.0, np.nan)
    out["bb_position"] = (close - out["bb_mid"]) / bb_width
    range_width = (out["hh20"] - out["ll20"]).replace(0.0, np.nan)
    out["range20_position"] = (close - out["ll20"]) / range_width
    candle_high = out[["open", "close"]].max(axis=1)
    candle_low = out[["open", "close"]].min(axis=1)
    out["body_atr"] = (out["close"] - out["open"]) / atr
    out["upper_wick_atr"] = (out["high"] - candle_high) / atr
    out["lower_wick_atr"] = (candle_low - out["low"]) / atr
    out["volume_ratio"] = out["tick_volume"] / out["volume_ma20"].replace(0.0, np.nan)
    hour = out["time"].dt.hour + (out["time"].dt.minute / 60.0)
    out["hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
    out["hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
    out[list(FEATURES)] = out[list(FEATURES)].replace([np.inf, -np.inf], np.nan)
    return out


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-values))


def _fit_logistic(x: np.ndarray, y: np.ndarray, iterations: int = 1800, lr: float = 0.04, l2: float = 0.01) -> tuple[np.ndarray, float]:
    weights = np.zeros(x.shape[1], dtype=float)
    bias = 0.0
    positives = max(1, int(y.sum()))
    negatives = max(1, len(y) - positives)
    sample_weights = np.where(y > 0.5, len(y) / (2.0 * positives), len(y) / (2.0 * negatives))
    weight_sum = float(sample_weights.sum())
    for _ in range(iterations):
        predicted = _sigmoid(x @ weights + bias)
        error = (predicted - y) * sample_weights
        weights -= lr * (((x.T @ error) / weight_sum) + (l2 * weights))
        bias -= lr * float(error.sum() / weight_sum)
    return weights, bias


def _select_indices(indices: np.ndarray, probabilities: np.ndarray, threshold: float, cooldown_bars: int = 6) -> list[int]:
    selected = []
    last = -999999
    for idx in indices:
        if probabilities[idx] < threshold or idx - last < cooldown_bars:
            continue
        selected.append(int(idx))
        last = int(idx)
    return selected


def _hit(side: str, level: float, high: float, low: float, kind: str) -> bool:
    if kind == "tp":
        return high >= level if side == "buy" else low <= level
    return low <= level if side == "buy" else high >= level


def _simulate_signal(
    symbol: str,
    rates: pd.DataFrame,
    idx: int,
    side: str,
    sl_atr: float,
    tp_plan: tuple[float, float, float],
    protect_be: bool,
    spread_price: float,
) -> float:
    entry = float(rates.iloc[idx]["close"])
    atr = float(rates.iloc[idx]["atr14"] or 0.0)
    if atr <= 0:
        return 0.0
    direction = 1.0 if side == "buy" else -1.0
    sl = entry - (direction * atr * sl_atr)
    tp1 = entry + (direction * atr * tp_plan[0])
    end_idx = min(len(rates), idx + 73)
    total = 0.0
    for target_mult in tp_plan:
        target = entry + (direction * atr * target_mult)
        current_sl = sl
        exit_price = float(rates.iloc[end_idx - 1]["close"])
        for bar_idx in range(idx + 1, end_idx):
            high = float(rates.iloc[bar_idx]["high"])
            low = float(rates.iloc[bar_idx]["low"])
            if _hit(side, current_sl, high, low, "sl"):
                exit_price = current_sl
                break
            if _hit(side, target, high, low, "tp"):
                exit_price = target
                break
            if protect_be and target_mult > tp_plan[0] and _hit(side, tp1, high, low, "tp"):
                current_sl = entry
        gross = _profit(symbol, side, entry, exit_price)
        spread = abs(_profit(symbol, "buy", entry, entry + spread_price))
        total += float(gross) - float(spread)
    return round(total, 2)


def _trade_summary(values: list[float]) -> dict[str, Any]:
    wins = sum(value > 0.01 for value in values)
    losses = sum(value < -0.01 for value in values)
    flat = len(values) - wins - losses
    return {
        "signals": len(values),
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "non_loss_rate_pct": round(100.0 * (wins + flat) / max(1, len(values)), 2),
        "pnl_001_per_leg": round(sum(values), 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--broker-time-offset-minutes", type=int, default=180)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        offset = timedelta(minutes=int(args.broker_time_offset_minutes))
        end = datetime.now(UTC) + offset
        start = end - timedelta(days=int(args.days) + 3)
        rates = _feature_frame(_rates(symbol, start, end))
        signals = await _fetch_signals(rates, int(args.days), int(args.broker_time_offset_minutes))
        valid_mask = rates[list(FEATURES)].notna().all(axis=1).to_numpy()
        valid_indices = np.flatnonzero(valid_mask)
        if len(signals) < 30:
            raise RuntimeError(f"Only {len(signals)} Phoenix signals found; need at least 30")

        side_by_index: dict[int, int] = {}
        for item in signals:
            if valid_mask[item.start_idx]:
                side_by_index[int(item.start_idx)] = 1 if item.signal.side == "buy" else 0
        positive_indices = np.array(sorted(side_by_index), dtype=int)
        split_time = rates.iloc[int(len(rates) * 0.70)]["time"]
        split_idx = int(rates["time"].searchsorted(split_time, side="left"))
        train_positive = positive_indices[positive_indices < split_idx]
        holdout_positive = positive_indices[positive_indices >= split_idx]
        if len(train_positive) < 20 or len(holdout_positive) < 5:
            raise RuntimeError("Insufficient chronological Phoenix split")

        x_all = rates.loc[:, FEATURES].to_numpy(dtype=float)
        train_valid = valid_indices[valid_indices < split_idx]
        exclusion = np.zeros(len(rates), dtype=bool)
        for idx in positive_indices:
            exclusion[max(0, idx - 6): min(len(rates), idx + 7)] = True
        negative_pool = train_valid[~exclusion[train_valid]]
        rng = np.random.default_rng(20260715)
        negative_count = min(len(negative_pool), len(train_positive) * 4)
        train_negative = np.sort(rng.choice(negative_pool, size=negative_count, replace=False))
        fit_indices = np.concatenate([train_positive, train_negative])
        y_setup = np.concatenate([np.ones(len(train_positive)), np.zeros(len(train_negative))])

        mean = np.nanmean(x_all[fit_indices], axis=0)
        std = np.nanstd(x_all[fit_indices], axis=0)
        std[std < 1e-8] = 1.0
        x_standard = (x_all - mean) / std
        setup_weights, setup_bias = _fit_logistic(x_standard[fit_indices], y_setup)
        side_y = np.array([side_by_index[int(idx)] for idx in train_positive], dtype=float)
        side_weights, side_bias = _fit_logistic(x_standard[train_positive], side_y, iterations=1400, lr=0.035)
        setup_prob = _sigmoid(x_standard @ setup_weights + setup_bias)
        side_prob = _sigmoid(x_standard @ side_weights + side_bias)

        train_probs = setup_prob[train_valid]
        thresholds = sorted({float(np.quantile(train_probs, q)) for q in (0.95, 0.97, 0.98, 0.99, 0.995)})
        info = mt5.symbol_info(symbol)
        point = float(getattr(info, "point", 0.01) or 0.01)
        spread_price = float(rates["spread"].median()) * point if "spread" in rates.columns else 0.0

        train_candidates = valid_indices[(valid_indices >= 200) & (valid_indices < split_idx)]
        holdout_candidates = valid_indices[valid_indices >= split_idx]
        configs = []
        for threshold in thresholds:
            train_selected = _select_indices(train_candidates, setup_prob, threshold)
            for sl_atr in (0.8, 1.2, 1.6):
                for plan_name, tp_plan in TP_PLANS.items():
                    for protect_be in (False, True):
                        values = [
                            _simulate_signal(symbol, rates, idx, "buy" if side_prob[idx] >= 0.5 else "sell", sl_atr, tp_plan, protect_be, spread_price)
                            for idx in train_selected
                        ]
                        configs.append(
                            {
                                "threshold": threshold,
                                "sl_atr": sl_atr,
                                "tp_plan_name": plan_name,
                                "tp_plan": tp_plan,
                                "protect_be_after_tp1": protect_be,
                                "train": _trade_summary(values),
                            }
                        )
        profitable = [row for row in configs if row["train"]["pnl_001_per_leg"] > 0 and row["train"]["signals"] >= 20]
        winner = max(
            profitable or configs,
            key=lambda row: (row["train"]["win_rate_pct"], row["train"]["pnl_001_per_leg"], row["train"]["signals"]),
        )
        holdout_selected = _select_indices(holdout_candidates, setup_prob, float(winner["threshold"]))
        holdout_values = [
            _simulate_signal(
                symbol,
                rates,
                idx,
                "buy" if side_prob[idx] >= 0.5 else "sell",
                float(winner["sl_atr"]),
                tuple(float(value) for value in winner["tp_plan"]),
                bool(winner["protect_be_after_tp1"]),
                spread_price,
            )
            for idx in holdout_selected
        ]
        winner["holdout"] = _trade_summary(holdout_values)

        def near_actual(selected: list[int], actual: np.ndarray) -> int:
            actual_set = set(int(value) for value in actual)
            return sum(any((idx + delta) in actual_set for delta in (-1, 0, 1)) for idx in selected)

        train_selected = _select_indices(train_candidates, setup_prob, float(winner["threshold"]))
        train_matches = near_actual(train_selected, train_positive)
        holdout_matches = near_actual(holdout_selected, holdout_positive)
        coefficients = sorted(
            ({"feature": feature, "setup_weight": round(float(weight), 5)} for feature, weight in zip(FEATURES, setup_weights)),
            key=lambda row: abs(row["setup_weight"]),
            reverse=True,
        )
        buy_coefficients = sorted(
            ({"feature": feature, "buy_weight": round(float(weight), 5)} for feature, weight in zip(FEATURES, side_weights)),
            key=lambda row: abs(row["buy_weight"]),
            reverse=True,
        )
        signal_hours: dict[str, int] = {}
        for item in signals:
            hour = item.dt.astimezone(UTC).strftime("%H")
            signal_hours[hour] = signal_hours.get(hour, 0) + 1
        payload = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": rates.iloc[0]["time"].isoformat(), "end": rates.iloc[-1]["time"].isoformat()},
            "channel": "PHOENIX VIP",
            "symbol": symbol,
            "timeframe": "M5",
            "signals_found": len(signals),
            "unique_signal_bars": len(positive_indices),
            "train_signal_bars": len(train_positive),
            "holdout_signal_bars": len(holdout_positive),
            "split_time": split_time.isoformat(),
            "median_spread_price": round(spread_price, 4),
            "model": {
                "features": list(FEATURES),
                "feature_mean": [round(float(value), 8) for value in mean],
                "feature_std": [round(float(value), 8) for value in std],
                "setup_weights": [round(float(value), 8) for value in setup_weights],
                "setup_bias": round(float(setup_bias), 8),
                "side_weights": [round(float(value), 8) for value in side_weights],
                "side_bias": round(float(side_bias), 8),
            },
            "best_strategy": winner,
            "mimic_detection": {
                "train_generated": len(train_selected),
                "train_near_phoenix": train_matches,
                "train_precision_pct": round(100.0 * train_matches / max(1, len(train_selected)), 2),
                "holdout_generated": len(holdout_selected),
                "holdout_near_phoenix": holdout_matches,
                "holdout_precision_pct": round(100.0 * holdout_matches / max(1, len(holdout_selected)), 2),
                "holdout_recall_pct": round(100.0 * holdout_matches / max(1, len(holdout_positive)), 2),
            },
            "top_setup_features": coefficients[:10],
            "top_buy_sell_features": buy_coefficients[:10],
            "signal_hours_utc": dict(sorted(signal_hours.items())),
            "deployment_ready": bool(
                winner["holdout"]["signals"] >= 20
                and winner["holdout"]["pnl_001_per_leg"] > 0
                and winner["holdout"]["win_rate_pct"] >= 60.0
            ),
            "note": "Research model only. It is not enabled live unless holdout requirements are met.",
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: payload[key] for key in ("signals_found", "unique_signal_bars", "best_strategy", "mimic_detection", "deployment_ready")}, ensure_ascii=True), flush=True)
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
