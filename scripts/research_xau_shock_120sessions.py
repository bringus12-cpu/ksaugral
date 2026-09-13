from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExitConfig:
    sl_atr: float
    rr: float
    be_r: float
    trail_r: float
    max_hold: int


def _load_sessions(path: Path, sessions: int) -> tuple[pd.DataFrame, list[str]]:
    frame = pd.read_csv(path, compression="gzip")
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame = frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    session_key = (frame["time"] + pd.Timedelta(hours=2)).dt.date
    counts = frame.groupby(session_key).size()
    complete = [day for day, count in counts.items() if int(count) >= 180]
    if len(complete) < sessions:
        raise RuntimeError(f"Only {len(complete)} complete sessions; requested {sessions}")
    selected = complete[-sessions:]
    frame = frame[session_key.isin(selected)].reset_index(drop=True)
    frame["session"] = (frame["time"] + pd.Timedelta(hours=2)).dt.date.astype(str)
    return frame, [day.isoformat() for day in selected]


def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    close = out["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1.0 / 30.0, adjust=False).mean().shift(1)
    out["ema20"] = close.ewm(span=20, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["vol_med"] = out["tick_volume"].rolling(60, min_periods=30).median().shift(1)
    out["range30"] = high.shift(1).rolling(30, min_periods=30).max() - low.shift(1).rolling(30, min_periods=30).min()
    out["atr_med120"] = out["atr"].rolling(120, min_periods=60).median().shift(1)
    return out


def _dedupe(indices: np.ndarray, cooldown: int) -> np.ndarray:
    kept: list[int] = []
    next_allowed = -1
    for idx in indices.tolist():
        if idx >= next_allowed:
            kept.append(int(idx))
            next_allowed = int(idx) + cooldown
    return np.asarray(kept, dtype=np.int64)


def _continuation_signals(frame: pd.DataFrame, lookback: int, threshold: float, efficiency: float, trend: bool) -> list[tuple[int, int]]:
    close = frame["close"].to_numpy(float)
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    atr = frame["atr"].to_numpy(float)
    ema20 = frame["ema20"].to_numpy(float)
    ema50 = frame["ema50"].to_numpy(float)
    move = close - np.roll(close, lookback)
    path = pd.Series(np.abs(np.diff(close, prepend=close[0]))).rolling(lookback).sum().to_numpy()
    eff = np.abs(move) / np.maximum(path, 1e-9)
    close_pos_buy = (close - low) / np.maximum(high - low, 1e-9)
    close_pos_sell = (high - close) / np.maximum(high - low, 1e-9)
    valid = np.arange(len(frame)) >= max(120, lookback)
    raw = valid & (np.abs(move) >= threshold * atr) & (eff >= efficiency)
    raw &= np.where(move > 0, close_pos_buy >= 0.60, close_pos_sell >= 0.60)
    if trend:
        raw &= np.where(move > 0, ema20 > ema50, ema20 < ema50)
    idxs = _dedupe(np.flatnonzero(raw), max(10, lookback * 2))
    return [(int(idx), 1 if move[idx] > 0 else -1) for idx in idxs if idx + 1 < len(frame)]


def _fade_signals(frame: pd.DataFrame, lookback: int, threshold: float, rejection: float) -> list[tuple[int, int]]:
    close = frame["close"].to_numpy(float)
    open_ = frame["open"].to_numpy(float)
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    atr = frame["atr"].to_numpy(float)
    move = close - np.roll(close, lookback)
    candle_range = np.maximum(high - low, 1e-9)
    upper_wick = (high - np.maximum(open_, close)) / candle_range
    lower_wick = (np.minimum(open_, close) - low) / candle_range
    valid = np.arange(len(frame)) >= max(120, lookback)
    raw = valid & (np.abs(move) >= threshold * atr)
    raw &= np.where(move > 0, upper_wick >= rejection, lower_wick >= rejection)
    idxs = _dedupe(np.flatnonzero(raw), max(15, lookback * 2))
    return [(int(idx), -1 if move[idx] > 0 else 1) for idx in idxs if idx + 1 < len(frame)]


def _pullback_signals(frame: pd.DataFrame, lookback: int, threshold: float, retrace: float, wait: int) -> list[tuple[int, int]]:
    close = frame["close"].to_numpy(float)
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    atr = frame["atr"].to_numpy(float)
    move = close - np.roll(close, lookback)
    events = _dedupe(
        np.flatnonzero((np.arange(len(frame)) >= max(120, lookback)) & (np.abs(move) >= threshold * atr)),
        max(15, lookback * 2),
    )
    signals: list[tuple[int, int]] = []
    next_allowed = -1
    for event in events.tolist():
        if event < next_allowed:
            continue
        side = 1 if move[event] > 0 else -1
        impulse = abs(float(move[event]))
        anchor = float(close[event])
        for idx in range(event + 1, min(len(frame) - 1, event + wait + 1)):
            if side > 0:
                touched = float(low[idx]) <= anchor - impulse * retrace
                resumed = float(close[idx]) > float(close[idx - 1])
            else:
                touched = float(high[idx]) >= anchor + impulse * retrace
                resumed = float(close[idx]) < float(close[idx - 1])
            if touched and resumed:
                signals.append((idx, side))
                next_allowed = idx + 15
                break
    return signals


def _breakout_signals(frame: pd.DataFrame, compression: float, breakout_atr: float) -> list[tuple[int, int]]:
    close = frame["close"].to_numpy(float)
    high_prev = frame["high"].shift(1).rolling(30, min_periods=30).max().to_numpy(float)
    low_prev = frame["low"].shift(1).rolling(30, min_periods=30).min().to_numpy(float)
    atr = frame["atr"].to_numpy(float)
    range30 = frame["range30"].to_numpy(float)
    atr_med = frame["atr_med120"].to_numpy(float)
    compressed = range30 <= compression * atr_med * np.sqrt(30.0)
    buy = compressed & (close >= high_prev + breakout_atr * atr)
    sell = compressed & (close <= low_prev - breakout_atr * atr)
    idxs = _dedupe(np.flatnonzero(buy | sell), 30)
    return [(int(idx), 1 if buy[idx] else -1) for idx in idxs if idx + 1 < len(frame)]


def _simulate(
    frame: pd.DataFrame,
    signals: list[tuple[int, int]],
    cfg: ExitConfig,
    commission: float,
) -> list[dict[str, Any]]:
    opens = frame["open"].to_numpy(float)
    highs = frame["high"].to_numpy(float)
    lows = frame["low"].to_numpy(float)
    closes = frame["close"].to_numpy(float)
    atrs = frame["atr"].to_numpy(float)
    spreads = frame["spread"].to_numpy(float) * 0.01
    times = frame["time"].tolist()
    sessions = frame["session"].tolist()
    trades: list[dict[str, Any]] = []
    next_free = -1
    for signal_idx, side in signals:
        entry_idx = signal_idx + 1
        if entry_idx <= next_free or entry_idx >= len(frame) or not np.isfinite(atrs[signal_idx]):
            continue
        spread = max(0.0, float(spreads[entry_idx]))
        raw_entry = float(opens[entry_idx])
        entry = raw_entry + spread if side > 0 else raw_entry
        risk = max(0.35, float(atrs[signal_idx]) * cfg.sl_atr)
        sl = entry - risk if side > 0 else entry + risk
        tp = entry + risk * cfg.rr if side > 0 else entry - risk * cfg.rr
        initial_sl = sl
        best = 0.0
        reason = "timeout"
        exit_price = float(closes[min(len(frame) - 1, entry_idx + cfg.max_hold)])
        exit_idx = min(len(frame) - 1, entry_idx + cfg.max_hold)
        for idx in range(entry_idx, min(len(frame), entry_idx + cfg.max_hold + 1)):
            bar_high = float(highs[idx])
            bar_low = float(lows[idx])
            if side < 0:
                bar_high += float(spreads[idx])
                bar_low += float(spreads[idx])
            sl_hit = bar_low <= sl if side > 0 else bar_high >= sl
            tp_hit = bar_high >= tp if side > 0 else bar_low <= tp
            if sl_hit or tp_hit:
                reason = "sl" if sl_hit else "tp"
                exit_price = sl if sl_hit else tp
                exit_idx = idx
                break
            favorable = bar_high - entry if side > 0 else entry - bar_low
            best = max(best, favorable)
            new_sl = sl
            if cfg.be_r > 0 and best >= risk * cfg.be_r:
                new_sl = max(new_sl, entry) if side > 0 else min(new_sl, entry)
            if cfg.trail_r > 0 and best >= risk:
                candidate = (entry + best - risk * cfg.trail_r) if side > 0 else (entry - best + risk * cfg.trail_r)
                new_sl = max(new_sl, candidate) if side > 0 else min(new_sl, candidate)
            sl = new_sl
        move = (exit_price - entry) if side > 0 else (entry - exit_price)
        pnl = move - commission
        trades.append({
            "signal_time": times[signal_idx].isoformat(),
            "opened": times[entry_idx].isoformat(),
            "closed": times[exit_idx].isoformat(),
            "session": sessions[entry_idx],
            "side": "buy" if side > 0 else "sell",
            "entry": round(entry, 3),
            "exit": round(exit_price, 3),
            "risk": round(risk, 3),
            "pnl_001": round(pnl, 2),
            "reason": reason,
        })
        next_free = exit_idx
    return trades


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = np.asarray([float(t["pnl_001"]) for t in trades], dtype=float)
    if not len(pnl):
        return {"trades": 0, "pnl_001": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0, "max_dd_001": 0.0}
    equity = np.cumsum(pnl)
    peaks = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    dd = equity - peaks
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    return {
        "trades": int(len(pnl)),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 2),
        "pnl_001": round(float(pnl.sum()), 2),
        "average_001": round(float(pnl.mean()), 3),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else 99.0,
        "max_dd_001": round(abs(float(dd.min())), 2),
    }


def _mega_move_stats(frame: pd.DataFrame, trades: list[dict[str, Any]]) -> dict[str, Any]:
    close = frame["close"].astype(float)
    future_move = close.shift(-15) - close
    threshold = float(future_move.abs().quantile(0.95))
    mega = frame.loc[future_move.abs() >= threshold, ["time", "session"]].copy()
    mega["direction"] = np.where(future_move.loc[mega.index] > 0, "buy", "sell")
    mega = mega.groupby("session", as_index=False).first()
    opened = pd.DataFrame(trades)
    captured = 0
    if not opened.empty:
        opened["opened"] = pd.to_datetime(opened["opened"], utc=True)
        for _, event in mega.iterrows():
            near = opened[(opened["opened"] >= event["time"] - pd.Timedelta(minutes=5)) & (opened["opened"] <= event["time"] + pd.Timedelta(minutes=10))]
            if (near["side"] == event["direction"]).any():
                captured += 1
    return {
        "definition": "first 15-minute absolute close move in the top 5% tail, one event per session",
        "threshold_usd": round(threshold, 3),
        "events": int(len(mega)),
        "captured_directionally": int(captured),
        "capture_rate_pct": round(captured / max(1, len(mega)) * 100.0, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=120)
    parser.add_argument("--train-sessions", type=int, default=80)
    parser.add_argument("--history", default="data_vantage/history/xau_300_sessions/xauusd_plus_m1_300s.csv.gz")
    parser.add_argument("--output", default="data_vantage/xau_shock_research_120sessions.json")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    frame, session_dates = _load_sessions(Path(args.history), args.sessions)
    frame = _indicators(frame)
    train_sessions = set(session_dates[: args.train_sessions])
    holdout_sessions = set(session_dates[args.train_sessions :])

    signal_sets: list[tuple[str, dict[str, Any], list[tuple[int, int]]]] = []
    for lookback, threshold, efficiency, trend in itertools.product([3, 5, 10, 15], [1.5, 2.0, 2.5, 3.0], [0.55, 0.70], [False, True]):
        params = {"lookback": lookback, "threshold_atr": threshold, "efficiency": efficiency, "trend": trend}
        signal_sets.append(("continuation", params, _continuation_signals(frame, lookback, threshold, efficiency, trend)))
    for lookback, threshold, retrace, wait in itertools.product([5, 10, 15], [1.75, 2.25, 2.75], [0.25, 0.40, 0.55], [10, 20]):
        params = {"lookback": lookback, "threshold_atr": threshold, "retrace": retrace, "wait": wait}
        signal_sets.append(("pullback", params, _pullback_signals(frame, lookback, threshold, retrace, wait)))
    for lookback, threshold, rejection in itertools.product([3, 5, 10], [2.0, 2.5, 3.0], [0.35, 0.50, 0.65]):
        params = {"lookback": lookback, "threshold_atr": threshold, "rejection": rejection}
        signal_sets.append(("fade", params, _fade_signals(frame, lookback, threshold, rejection)))
    for compression, breakout_atr in itertools.product([0.8, 1.0, 1.2], [0.10, 0.25, 0.40]):
        params = {"compression": compression, "breakout_atr": breakout_atr}
        signal_sets.append(("compression_breakout", params, _breakout_signals(frame, compression, breakout_atr)))

    if args.quick:
        exits = [
            ExitConfig(0.8, 1.0, 0.0, 0.0, 30),
            ExitConfig(1.1, 1.5, 1.0, 0.0, 30),
            ExitConfig(1.5, 2.5, 1.0, 0.0, 30),
        ]
    else:
        exits = [
            ExitConfig(*values)
            for values in itertools.product(
                [0.8, 1.1, 1.5],
                [1.0, 1.5, 2.5],
                [0.0, 1.0],
                [0.0],
                [30],
            )
        ]
    results: list[dict[str, Any]] = []
    for family, params, signals in signal_sets:
        for exit_cfg in exits:
            trades = _simulate(frame, signals, exit_cfg, commission=0.06)
            train = [t for t in trades if t["session"] in train_sessions]
            holdout = [t for t in trades if t["session"] in holdout_sessions]
            train_summary = _summary(train)
            holdout_summary = _summary(holdout)
            robust = (
                train_summary["trades"] >= 25
                and holdout_summary["trades"] >= 10
                and train_summary["pnl_001"] > 0
                and holdout_summary["pnl_001"] > 0
                and train_summary["profit_factor"] >= 1.05
                and holdout_summary["profit_factor"] >= 1.05
            )
            results.append({
                "family": family,
                "signal": params,
                "exit": exit_cfg.__dict__,
                "train": train_summary,
                "holdout": holdout_summary,
                "full": _summary(trades),
                "robust": robust,
                "score": round(
                    min(train_summary["profit_factor"], holdout_summary["profit_factor"]) * 100.0
                    + holdout_summary["pnl_001"]
                    - holdout_summary["max_dd_001"] * 0.25,
                    4,
                ),
            })

    robust = [row for row in results if row["robust"]]
    robust.sort(key=lambda row: row["score"], reverse=True)
    by_family: dict[str, Any] = {}
    best_any_by_family: dict[str, Any] = {}
    for family in sorted({row["family"] for row in results}):
        candidates = [row for row in results if row["family"] == family and row["robust"]]
        candidates.sort(key=lambda row: row["score"], reverse=True)
        by_family[family] = candidates[:3]
        any_candidates = [row for row in results if row["family"] == family]
        any_candidates.sort(
            key=lambda row: (
                row["train"]["pnl_001"] > 0,
                row["holdout"]["pnl_001"] > 0,
                min(row["train"]["profit_factor"], row["holdout"]["profit_factor"]),
                row["score"],
            ),
            reverse=True,
        )
        best_any_by_family[family] = any_candidates[:3]

    best = robust[0] if robust else None
    best_trades: list[dict[str, Any]] = []
    mega = None
    if best:
        matching = next(item for item in signal_sets if item[0] == best["family"] and item[1] == best["signal"])
        best_trades = _simulate(frame, matching[2], ExitConfig(**best["exit"]), commission=0.06)
        mega = _mega_move_stats(frame, best_trades)

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "sessions": session_dates,
        "split": {"train": session_dates[: args.train_sessions], "holdout": session_dates[args.train_sessions :]},
        "method": {
            "execution": "causal closed-M1 signal, entry at next M1 open, historical spread, USD 0.06 commission per 0.01 lot, SL-first ambiguity, one trade at a time per model",
            "selection": "configuration selected on first 80 sessions; final 40 sessions retained as chronological holdout",
            "pnl_unit": "USD at 0.01 XAU lot",
        },
        "tested_signal_models": len(signal_sets),
        "tested_total_configurations": len(results),
        "robust_configurations": len(robust),
        "best": best,
        "best_by_family": by_family,
        "best_any_by_family": best_any_by_family,
        "best_mega_move_capture": mega,
        "best_trade_log": best_trades,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({k: v for k, v in output.items() if k not in {"sessions", "best_trade_log"}}, indent=2))
    print(path)


if __name__ == "__main__":
    main()
