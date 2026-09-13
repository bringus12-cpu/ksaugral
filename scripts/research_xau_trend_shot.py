from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.indicators import enrich, ema
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown


def _rates(symbol: str, timeframe: int, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, timeframe, start, end)
    if raw is None or len(raw) == 0:
        raw = mt5.copy_rates_from_pos(symbol, timeframe, 0, 99_999)
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"No rates for timeframe={timeframe}: {mt5.last_error()}")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    frame = frame[(frame["time"] >= pd.Timestamp(start)) & (frame["time"] <= pd.Timestamp(end))]
    frame = enrich(frame.sort_values("time").reset_index(drop=True))
    frame["ema9"] = ema(frame["close"], 9)

    high, low, close = frame["high"], frame["low"], frame["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    frame["plus_di14"] = 100.0 * plus_dm.ewm(alpha=1.0 / 14, adjust=False).mean() / atr14
    frame["minus_di14"] = 100.0 * minus_dm.ewm(alpha=1.0 / 14, adjust=False).mean() / atr14
    return frame


def _aligned(m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame) -> pd.DataFrame:
    base = m1.copy()
    higher_frames = []
    for source, minutes, prefix in ((m5, 5, "m5"), (m15, 15, "m15")):
        available = source.copy()
        available["time"] = available["time"] + pd.Timedelta(minutes=minutes)
        columns = [
            "time",
            "open",
            "high",
            "low",
            "close",
            "ema9",
            "ema20",
            "ema50",
            "ema200",
            "rsi14",
            "atr14",
            "adx14",
            "plus_di14",
            "minus_di14",
        ]
        available = available[columns].rename(
            columns={column: f"{prefix}_{column}" for column in columns if column != "time"}
        )
        higher_frames.append(available)
    for frame in [base, *higher_frames]:
        frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.as_unit("ns")
    for higher in higher_frames:
        base = pd.merge_asof(
            base.sort_values("time"),
            higher.sort_values("time"),
            on="time",
            direction="backward",
        )
    return base.reset_index(drop=True)


def _signal(frame: pd.DataFrame, idx: int, cfg: dict[str, float]) -> str | None:
    row = frame.iloc[idx]
    prev = frame.iloc[idx - 1]
    required = [
        "m5_ema9",
        "m5_ema20",
        "m5_ema50",
        "m5_adx14",
        "m5_plus_di14",
        "m5_minus_di14",
        "m15_ema20",
        "m15_ema50",
        "m15_adx14",
        "m15_plus_di14",
        "m15_minus_di14",
    ]
    if any(pd.isna(row.get(column)) for column in required):
        return None

    body = abs(float(row["close"]) - float(row["open"]))
    atr1 = float(row["atr14"])
    if atr1 <= 0 or body < atr1 * cfg["body_atr"]:
        return None

    macro_buy = (
        float(row["m15_ema20"]) > float(row["m15_ema50"])
        and float(row["m15_adx14"]) >= cfg["adx15"]
        and float(row["m15_plus_di14"]) > float(row["m15_minus_di14"]) + cfg["di_gap"]
    )
    macro_sell = (
        float(row["m15_ema20"]) < float(row["m15_ema50"])
        and float(row["m15_adx14"]) >= cfg["adx15"]
        and float(row["m15_minus_di14"]) > float(row["m15_plus_di14"]) + cfg["di_gap"]
    )
    mini_buy = (
        float(row["m5_ema9"]) > float(row["m5_ema20"]) > float(row["m5_ema50"])
        and float(row["m5_close"]) > float(row["m5_ema9"])
        and float(row["m5_adx14"]) >= cfg["adx5"]
        and float(row["m5_plus_di14"]) > float(row["m5_minus_di14"])
    )
    mini_sell = (
        float(row["m5_ema9"]) < float(row["m5_ema20"]) < float(row["m5_ema50"])
        and float(row["m5_close"]) < float(row["m5_ema9"])
        and float(row["m5_adx14"]) >= cfg["adx5"]
        and float(row["m5_minus_di14"]) > float(row["m5_plus_di14"])
    )

    close = float(row["close"])
    open_price = float(row["open"])
    ema9_now = float(row["ema9"])
    ema9_prev = float(prev["ema9"])
    pullback = cfg["pullback_atr"] * atr1
    buy_trigger = (
        macro_buy
        and mini_buy
        and float(prev["close"]) <= ema9_prev + pullback
        and close > ema9_now
        and close > open_price
        and cfg["rsi"] <= float(row["rsi14"]) <= 72.0
    )
    sell_trigger = (
        macro_sell
        and mini_sell
        and float(prev["close"]) >= ema9_prev - pullback
        and close < ema9_now
        and close < open_price
        and 28.0 <= float(row["rsi14"]) <= 100.0 - cfg["rsi"]
    )
    if buy_trigger == sell_trigger:
        return None
    return "buy" if buy_trigger else "sell"


def _simulate(
    frame: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cfg: dict[str, float],
    spread: float,
    lot: float,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    next_allowed = start
    open_trade: dict[str, Any] | None = None
    equity = peak = max_dd = 0.0
    max_loss_streak = loss_streak = 0

    for idx in range(250, len(frame) - 1):
        row = frame.iloc[idx]
        ts = row["time"]
        if ts < start:
            continue
        if ts > end:
            break

        if open_trade:
            side = open_trade["side"]
            high, low = float(row["high"]), float(row["low"])
            favorable = high - open_trade["entry"] if side == "buy" else open_trade["entry"] - low
            if cfg["be_atr"] > 0 and favorable >= open_trade["risk"] * cfg["be_atr"]:
                candidate = open_trade["entry"] + spread if side == "buy" else open_trade["entry"] - spread
                open_trade["sl"] = max(open_trade["sl"], candidate) if side == "buy" else min(open_trade["sl"], candidate)
            sl_hit = low <= open_trade["sl"] if side == "buy" else high >= open_trade["sl"]
            tp_hit = high >= open_trade["tp"] if side == "buy" else low <= open_trade["tp"]
            timed_out = idx - open_trade["opened_idx"] >= int(cfg["max_hold"])
            exit_price = open_trade["sl"] if sl_hit else open_trade["tp"] if tp_hit else float(row["close"]) if timed_out else None
            if exit_price is not None:
                move = exit_price - open_trade["entry"] if side == "buy" else open_trade["entry"] - exit_price
                pnl = (move - spread) * (lot / 0.01)
                equity += pnl
                peak = max(peak, equity)
                max_dd = min(max_dd, equity - peak)
                loss_streak = loss_streak + 1 if pnl < 0 else 0
                max_loss_streak = max(max_loss_streak, loss_streak)
                trades.append(
                    {
                        "opened": str(open_trade["opened"]),
                        "closed": str(ts),
                        "side": side,
                        "entry": round(open_trade["entry"], 3),
                        "exit": round(exit_price, 3),
                        "pnl": round(pnl, 2),
                        "reason": "sl" if sl_hit else "tp" if tp_hit else "timeout",
                    }
                )
                open_trade = None
                next_allowed = ts + pd.Timedelta(minutes=int(cfg["cooldown"]))
            continue

        if ts < next_allowed:
            continue
        side = _signal(frame, idx, cfg)
        if not side:
            continue

        entry_row = frame.iloc[idx + 1]
        entry = float(entry_row["open"])
        atr5 = float(row["m5_atr14"])
        risk = min(cfg["sl_max"], max(cfg["sl_min"], atr5 * cfg["sl_atr5"]))
        sl = entry - risk if side == "buy" else entry + risk
        tp = entry + risk * cfg["rr"] if side == "buy" else entry - risk * cfg["rr"]
        open_trade = {
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "risk": risk,
            "opened": entry_row["time"],
            "opened_idx": idx + 1,
        }

    wins = sum(1 for trade in trades if trade["pnl"] > 0)
    losses = sum(1 for trade in trades if trade["pnl"] < 0)
    gross_profit = sum(max(0.0, trade["pnl"]) for trade in trades)
    gross_loss = abs(sum(min(0.0, trade["pnl"]) for trade in trades))
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / max(1, wins + losses) * 100.0, 2),
        "net_profit": round(equity, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else 0.0,
        "max_loss_streak": max_loss_streak,
        "trade_log": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--holdout-days", type=int, default=30)
    parser.add_argument("--lot", type=float, default=0.10)
    parser.add_argument("--output", default="data_vantage/xau_trend_shot_research_100d.json")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    login = int(os.environ["MT5_LOGIN"])
    connect(
        Mt5Credentials(
            login=login,
            password=os.environ["MT5_PASSWORD"],
            server=os.environ["MT5_SERVER"],
            path=os.environ.get("MT5_PATH"),
        )
    )
    symbol = ensure_symbol(os.getenv("SYMBOL", "XAUUSD"))
    tick = mt5.symbol_info_tick(symbol)
    spread = max(0.0, float(tick.ask) - float(tick.bid))
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    split = end - timedelta(days=args.holdout_days)
    warmup = timedelta(days=8)
    frame = _aligned(
        _rates(symbol, mt5.TIMEFRAME_M1, start - warmup, end),
        _rates(symbol, mt5.TIMEFRAME_M5, start - warmup, end),
        _rates(symbol, mt5.TIMEFRAME_M15, start - warmup, end),
    )

    keys = ["adx15", "adx5", "di_gap", "rsi", "body_atr", "pullback_atr", "sl_atr5", "rr", "be_atr"]
    grid = list(itertools.product(
        [18.0, 24.0],
        [18.0],
        [2.0],
        [52.0],
        [0.20],
        [0.20],
        [0.8],
        [0.8],
        [0.5],
    ))
    rows = []
    for values in grid:
        cfg = {
            **dict(zip(keys, values)),
            "sl_min": 2.0,
            "sl_max": 6.0,
            "max_hold": 30.0,
            "cooldown": 15.0,
        }
        train = _simulate(frame, pd.Timestamp(start), pd.Timestamp(split), cfg, spread, args.lot)
        test = _simulate(frame, pd.Timestamp(split), pd.Timestamp(end), cfg, spread, args.lot)
        score = (
            min(train["profit_factor"], test["profit_factor"]) * 10.0
            + min(train["win_rate_pct"], test["win_rate_pct"]) / 10.0
            + min(train["trades"], test["trades"] * 2) / 20.0
        )
        rows.append(
            {
                "config": cfg,
                "train": train,
                "holdout": test,
                "qualified": train["trades"] >= 12 and train["net_profit"] > 0 and test["net_profit"] > 0,
                "robust_score": round(score, 4),
            }
        )
    rows.sort(
        key=lambda row: (
            row["holdout"]["net_profit"] > 0,
            row["train"]["net_profit"] > 0,
            row["robust_score"],
        ),
        reverse=True,
    )
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "spread": round(spread, 5),
        "lot": args.lot,
        "range": {"start": start.isoformat(), "split": split.isoformat(), "end": end.isoformat()},
        "method": "closed M15/M5/M1, next-M1-open execution, current spread, SL-first ambiguity, one open trade",
        "tested_configurations": len(grid),
        "qualified_configurations": sum(1 for row in rows if row["qualified"]),
        "best": rows[:20],
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    if rows:
        summary = [{**row["config"], "train": {k: v for k, v in row["train"].items() if k != "trade_log"}, "holdout": {k: v for k, v in row["holdout"].items() if k != "trade_log"}} for row in rows[:5]]
        print(json.dumps(summary, indent=2))
    shutdown()


if __name__ == "__main__":
    main()
