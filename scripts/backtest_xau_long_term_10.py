from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown


STRATEGY_NAMES = {
    "trend_pullback_h4": "Trend D1 + pullback H4",
    "donchian_breakout": "Donchian breakout D1",
    "time_series_momentum": "Time-series momentum",
    "breakout_retest": "Breakout + retest D1",
    "volatility_squeeze": "Volatility squeeze",
    "macro_yield_usd": "Real yield + USD macro trend",
    "cot_extreme": "COT extreme + price confirmation",
    "gld_flow": "GLD flow momentum",
    "seasonality_trend": "Seasonality + trend",
    "gold_silver_ratio": "Gold/Silver ratio (XAU leg)",
}


def _download(url: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 100:
        return path
    request = urllib.request.Request(url, headers={"User-Agent": "xao-graal-research/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        path.write_bytes(response.read())
    return path


def _rates(symbol: str, timeframe: int, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, timeframe, start, end)
    if raw is None or len(raw) == 0:
        raw = mt5.copy_rates_from_pos(symbol, timeframe, 0, 99_999)
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"No MT5 rates for {symbol}, timeframe={timeframe}: {mt5.last_error()}")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    frame = frame[(frame["time"] >= pd.Timestamp(start)) & (frame["time"] <= pd.Timestamp(end))]
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _fred(cache: Path, series: str) -> pd.DataFrame:
    path = _download(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}",
        cache / f"fred_{series}.csv",
    )
    frame = pd.read_csv(path)
    frame.columns = ["date", series]
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    frame[series] = pd.to_numeric(frame[series], errors="coerce")
    frame = frame.dropna().sort_values("date")
    # Daily observations are treated as available at the next UTC session boundary.
    frame["available"] = frame["date"] + pd.Timedelta(days=1)
    return frame[["available", series]]


def _rolling_last_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    return values.rolling(window, min_periods=minimum).apply(
        lambda sample: float(pd.Series(sample).rank(pct=True).iloc[-1]),
        raw=False,
    )


def _cot(cache: Path, first_year: int, last_year: int) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for year in range(first_year, last_year + 1):
        path = _download(
            f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip",
            cache / f"cftc_disagg_{year}.zip",
        )
        with zipfile.ZipFile(path) as archive:
            member = next(name for name in archive.namelist() if name.lower().endswith("f_year.txt"))
            frame = pd.read_csv(archive.open(member), low_memory=False)
        market = frame["Market_and_Exchange_Names"].astype(str).str.strip()
        frame = frame[market.eq("GOLD - COMMODITY EXCHANGE INC.")].copy()
        rows.append(frame)
    cot = pd.concat(rows, ignore_index=True)
    cot["report_date"] = pd.to_datetime(cot["Report_Date_as_YYYY-MM-DD"], utc=True)
    long = pd.to_numeric(cot["M_Money_Positions_Long_All"], errors="coerce")
    short = pd.to_numeric(cot["M_Money_Positions_Short_All"], errors="coerce")
    cot["managed_net"] = long - short
    cot = cot.dropna(subset=["managed_net"]).sort_values("report_date")
    cot["cot_percentile"] = _rolling_last_percentile(cot["managed_net"], 156, 52)
    # Tuesday positions are normally released Friday; no Tuesday-to-Friday look-ahead.
    cot["available"] = cot["report_date"] + pd.Timedelta(days=3)
    return cot[["available", "managed_net", "cot_percentile"]]


def _gld(cache: Path) -> pd.DataFrame:
    path = _download(
        "https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en&product=gld",
        cache / "spdr_gld_historical.xlsx",
    )
    frame = pd.read_excel(path, sheet_name="US GLD Historical Archive")
    frame["date"] = pd.to_datetime(frame["Date"], format="%d-%b-%Y", utc=True, errors="coerce")
    frame["gld_tonnes"] = pd.to_numeric(frame["Tonnes of Gold"], errors="coerce")
    frame = frame.dropna(subset=["date", "gld_tonnes"]).sort_values("date")
    frame["gld_flow_20d"] = frame["gld_tonnes"].diff(20)
    frame["available"] = frame["date"] + pd.Timedelta(days=1)
    return frame[["available", "gld_tonnes", "gld_flow_20d"]]


def _merge_available(base: pd.DataFrame, external: pd.DataFrame) -> pd.DataFrame:
    left = base.copy().sort_values("available")
    right = external.copy().sort_values("available")
    return pd.merge_asof(left, right, on="available", direction="backward")


def _daily_features(d1: pd.DataFrame) -> pd.DataFrame:
    out = d1.copy()
    out["available"] = out["time"] + pd.Timedelta(days=1)
    out["ema50_slope5"] = out["ema50"] - out["ema50"].shift(5)
    out["ret21"] = out["close"].pct_change(21)
    out["ret63"] = out["close"].pct_change(63)
    out["ret126"] = out["close"].pct_change(126)
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"]
    out["bb_width_pct"] = _rolling_last_percentile(out["bb_width"], 100, 50)
    out["hh10"] = out["high"].rolling(10).max().shift(1)
    out["ll10"] = out["low"].rolling(10).min().shift(1)
    return out


def _candidate(strategy: str, row: pd.Series, side: str, timeframe: str) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "signal_time": pd.Timestamp(row["available"]),
        "side": side,
        "timeframe": timeframe,
        "signal_price": float(row["close"]),
        "atr": float(row["atr14"]),
    }


def _daily_candidates(d1: pd.DataFrame, test_start: pd.Timestamp, test_end: pd.Timestamp) -> dict[str, list[dict[str, Any]]]:
    result = {key: [] for key in STRATEGY_NAMES}
    regime_previous: dict[str, str | None] = {
        "time_series_momentum": None,
        "macro_yield_usd": None,
        "gld_flow": None,
    }

    for idx in range(252, len(d1)):
        row = d1.iloc[idx]
        prev = d1.iloc[idx - 1]
        if row["available"] < test_start or row["available"] > test_end:
            continue

        if row["close"] > row["hh20"] and prev["close"] <= prev["hh20"]:
            result["donchian_breakout"].append(_candidate("donchian_breakout", row, "buy", "D1"))
        elif row["close"] < row["ll20"] and prev["close"] >= prev["ll20"]:
            result["donchian_breakout"].append(_candidate("donchian_breakout", row, "sell", "D1"))

        score = sum(float(row[key]) > 0 for key in ("ret21", "ret63", "ret126"))
        momentum_side = "buy" if score >= 2 and row["close"] > row["ema200"] else "sell" if score <= 1 and row["close"] < row["ema200"] else None
        weekly_rebalance = pd.Timestamp(row["time"]).dayofweek == 0
        if momentum_side and (weekly_rebalance or momentum_side != regime_previous["time_series_momentum"]):
            result["time_series_momentum"].append(_candidate("time_series_momentum", row, momentum_side, "D1"))
        regime_previous["time_series_momentum"] = momentum_side

        prior_break_up = prev["close"] > prev["hh20"]
        prior_break_down = prev["close"] < prev["ll20"]
        if prior_break_up and row["low"] <= prev["hh20"] and row["close"] > prev["hh20"] and row["close"] > row["open"]:
            result["breakout_retest"].append(_candidate("breakout_retest", row, "buy", "D1"))
        elif prior_break_down and row["high"] >= prev["ll20"] and row["close"] < prev["ll20"] and row["close"] < row["open"]:
            result["breakout_retest"].append(_candidate("breakout_retest", row, "sell", "D1"))

        was_squeezed = float(prev["bb_width_pct"]) <= 0.20
        if was_squeezed and row["close"] > row["hh10"] and row["atr14"] > prev["atr14"]:
            result["volatility_squeeze"].append(_candidate("volatility_squeeze", row, "buy", "D1"))
        elif was_squeezed and row["close"] < row["ll10"] and row["atr14"] > prev["atr14"]:
            result["volatility_squeeze"].append(_candidate("volatility_squeeze", row, "sell", "D1"))

        macro_side = None
        if all(pd.notna(row.get(key)) for key in ("DFII10", "DFII10_ma20", "DTWEXBGS", "DTWEXBGS_ma50")):
            if row["DFII10"] < row["DFII10_ma20"] and row["DTWEXBGS"] < row["DTWEXBGS_ma50"] and row["close"] > row["ema50"]:
                macro_side = "buy"
            elif row["DFII10"] > row["DFII10_ma20"] and row["DTWEXBGS"] > row["DTWEXBGS_ma50"] and row["close"] < row["ema50"]:
                macro_side = "sell"
        if macro_side and (weekly_rebalance or macro_side != regime_previous["macro_yield_usd"]):
            result["macro_yield_usd"].append(_candidate("macro_yield_usd", row, macro_side, "D1"))
        regime_previous["macro_yield_usd"] = macro_side

        if pd.notna(row.get("cot_percentile")):
            crosses_up = prev["close"] <= prev["ema20"] and row["close"] > row["ema20"]
            crosses_down = prev["close"] >= prev["ema20"] and row["close"] < row["ema20"]
            if row["cot_percentile"] <= 0.10 and crosses_up:
                result["cot_extreme"].append(_candidate("cot_extreme", row, "buy", "D1"))
            elif row["cot_percentile"] >= 0.90 and crosses_down:
                result["cot_extreme"].append(_candidate("cot_extreme", row, "sell", "D1"))

        flow_side = None
        if pd.notna(row.get("gld_flow_20d")):
            if row["gld_flow_20d"] > 0 and row["close"] > row["ema50"]:
                flow_side = "buy"
            elif row["gld_flow_20d"] < 0 and row["close"] < row["ema50"]:
                flow_side = "sell"
        if flow_side and (weekly_rebalance or flow_side != regime_previous["gld_flow"]):
            result["gld_flow"].append(_candidate("gld_flow", row, flow_side, "D1"))
        regime_previous["gld_flow"] = flow_side

        month = pd.Timestamp(row["time"]).month
        season_active = month in {1, 8, 9}
        crosses_ema20_up = prev["close"] <= prev["ema20"] and row["close"] > row["ema20"]
        first_active_session = month != pd.Timestamp(prev["time"]).month
        if season_active and row["close"] > row["ema50"] and row["ema50_slope5"] > 0 and (crosses_ema20_up or first_active_session):
            result["seasonality_trend"].append(_candidate("seasonality_trend", row, "buy", "D1"))

        if pd.notna(row.get("gsr_z")) and pd.notna(prev.get("gsr_z")):
            if prev["gsr_z"] <= -1.5 and row["gsr_z"] > prev["gsr_z"] and row["close"] > row["ema20"]:
                result["gold_silver_ratio"].append(_candidate("gold_silver_ratio", row, "buy", "D1"))
            elif prev["gsr_z"] >= 1.5 and row["gsr_z"] < prev["gsr_z"] and row["close"] < row["ema20"]:
                result["gold_silver_ratio"].append(_candidate("gold_silver_ratio", row, "sell", "D1"))

    return result


def _h4_candidates(h4: pd.DataFrame, d1: pd.DataFrame, test_start: pd.Timestamp, test_end: pd.Timestamp) -> list[dict[str, Any]]:
    daily_context = d1[["available", "close", "ema50", "ema200", "ema50_slope5"]].rename(
        columns={key: f"d1_{key}" for key in ("close", "ema50", "ema200", "ema50_slope5")}
    )
    frame = h4.copy()
    frame["available"] = frame["time"] + pd.Timedelta(hours=4)
    frame = pd.merge_asof(frame.sort_values("available"), daily_context.sort_values("available"), on="available", direction="backward")
    candidates: list[dict[str, Any]] = []
    for idx in range(50, len(frame)):
        row, prev = frame.iloc[idx], frame.iloc[idx - 1]
        if row["available"] < test_start or row["available"] > test_end:
            continue
        buy_context = row["d1_close"] > row["d1_ema50"] > row["d1_ema200"] and row["d1_ema50_slope5"] > 0
        sell_context = row["d1_close"] < row["d1_ema50"] < row["d1_ema200"] and row["d1_ema50_slope5"] < 0
        buy_pullback = row["low"] <= row["ema20"] and row["close"] > row["ema20"] and row["close"] > row["open"] and row["close"] > prev["close"]
        sell_pullback = row["high"] >= row["ema20"] and row["close"] < row["ema20"] and row["close"] < row["open"] and row["close"] < prev["close"]
        if buy_context and buy_pullback:
            candidates.append(_candidate("trend_pullback_h4", row, "buy", "H4"))
        elif sell_context and sell_pullback:
            candidates.append(_candidate("trend_pullback_h4", row, "sell", "H4"))
    return candidates


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float, spread: float) -> float:
    adjusted_exit = exit_price - spread if side == "buy" else exit_price + spread
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, lot, entry, adjusted_exit)
    if value is None:
        info = mt5.symbol_info(symbol)
        contract = float(info.trade_contract_size) if info else 100.0
        move = adjusted_exit - entry if side == "buy" else entry - adjusted_exit
        value = move * contract * lot
    return float(value)


def _simulate_strategy(
    symbol: str,
    h1: pd.DataFrame,
    candidates: list[dict[str, Any]],
    lot: float,
    spread: float,
    max_hold_days: int,
    max_open_positions: int = 0,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    open_intervals: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    skipped_overlap = 0

    for candidate in sorted(candidates, key=lambda item: item["signal_time"]):
        entry_rows = h1.index[h1["time"] >= candidate["signal_time"]]
        if len(entry_rows) == 0:
            continue
        entry_idx = int(entry_rows[0])
        entry_time = pd.Timestamp(h1.iloc[entry_idx]["time"])
        active_positions = sum(opened <= entry_time <= closed for opened, closed in open_intervals)
        if max_open_positions > 0 and active_positions >= max_open_positions:
            skipped_overlap += 1
            continue
        entry = float(h1.iloc[entry_idx]["open"])
        side = candidate["side"]
        # D1 ATR is scaled to a practical swing stop; H4 ATR is used directly.
        atr = float(candidate["atr"])
        risk = max(3.0, atr * (1.5 if candidate["timeframe"] == "H4" else 0.45))
        sl = entry - risk if side == "buy" else entry + risk
        tp = entry + 2.0 * risk if side == "buy" else entry - 2.0 * risk
        best = entry
        exit_price = float(h1.iloc[-1]["close"])
        exit_time = pd.Timestamp(h1.iloc[-1]["time"])
        reason = "end_of_test"
        be_armed = False
        trail_armed = False
        deadline = entry_time + pd.Timedelta(days=max_hold_days)

        for idx in range(entry_idx, len(h1)):
            bar = h1.iloc[idx]
            ts = pd.Timestamp(bar["time"])
            high, low = float(bar["high"]), float(bar["low"])
            sl_hit = low <= sl if side == "buy" else high >= sl
            tp_hit = high >= tp if side == "buy" else low <= tp
            if sl_hit or tp_hit:
                # When both prices occur in one H1 candle, use the adverse ordering.
                exit_price = sl if sl_hit else tp
                exit_time = ts
                reason = "sl" if sl_hit else "tp"
                break
            if ts >= deadline:
                exit_price = float(bar["close"])
                exit_time = ts
                reason = "time_exit"
                break

            if side == "buy":
                best = max(best, high)
                favorable = best - entry
            else:
                best = min(best, low)
                favorable = entry - best
            # Stop changes become active from the next H1 candle only.
            if favorable >= risk:
                sl = max(sl, entry) if side == "buy" else min(sl, entry)
                be_armed = True
            if favorable >= 1.5 * risk:
                trail = best - risk if side == "buy" else best + risk
                sl = max(sl, trail) if side == "buy" else min(sl, trail)
                trail_armed = True

        pnl = _profit(symbol, side, lot, entry, exit_price, spread)
        open_intervals.append((entry_time, exit_time))
        trades.append(
            {
                "signal_time": candidate["signal_time"].isoformat(),
                "entry_time": entry_time.isoformat(),
                "exit_time": exit_time.isoformat(),
                "side": side,
                "entry": round(entry, 3),
                "exit": round(exit_price, 3),
                "initial_sl": round(entry - risk if side == "buy" else entry + risk, 3),
                "target": round(tp, 3),
                "reason": reason,
                "be_armed": be_armed,
                "trail_armed": trail_armed,
                "pnl": round(pnl, 2),
            }
        )

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += float(trade["pnl"])
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    wins = sum(float(trade["pnl"]) > 0.005 for trade in trades)
    losses = sum(float(trade["pnl"]) < -0.005 for trade in trades)
    breakeven = len(trades) - wins - losses
    gross_profit = sum(max(0.0, float(trade["pnl"])) for trade in trades)
    gross_loss = abs(sum(min(0.0, float(trade["pnl"])) for trade in trades))
    return {
        "raw_signals": len(candidates),
        "executed_setups": len(trades),
        "skipped_while_position_open": skipped_overlap,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "net_pnl_usd": round(equity, 2),
        "average_trade_usd": round(equity / max(1, len(trades)), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_usd": round(max_dd, 2),
        "longs": sum(trade["side"] == "buy" for trade in trades),
        "shorts": sum(trade["side"] == "sell" for trade in trades),
        "trades": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--lot", type=float, default=0.01)
    parser.add_argument("--max-hold-days", type=int, default=14)
    parser.add_argument(
        "--max-open-per-strategy",
        type=int,
        default=0,
        help="0 means unlimited concurrent positions",
    )
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGY_NAMES),
        help="Comma-separated strategy keys",
    )
    parser.add_argument("--output", default="data_vantage/xau_long_term_10_strategies_60sessions.json")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / args.env, override=True)
    cache = root / "data_vantage" / "research_sources"
    now = datetime.now(UTC)
    history_start = now - timedelta(days=900)
    connect(
        Mt5Credentials(
            login=int(os.environ["MT5_LOGIN"]),
            password=os.environ["MT5_PASSWORD"],
            server=os.environ["MT5_SERVER"],
            path=os.environ.get("MT5_PATH"),
        )
    )
    try:
        xau = ensure_symbol(os.getenv("SYMBOL", "XAUUSD"))
        xag = ensure_symbol("XAGUSD")
        d1 = _daily_features(_rates(xau, mt5.TIMEFRAME_D1, history_start, now))
        h4 = _rates(xau, mt5.TIMEFRAME_H4, history_start, now)
        h1 = _rates(xau, mt5.TIMEFRAME_H1, history_start, now)
        silver = _rates(xag, mt5.TIMEFRAME_D1, history_start, now)

        today_utc = pd.Timestamp(now).floor("D")
        completed = d1[d1["time"] < today_utc]
        if len(completed) < args.sessions + 252:
            raise RuntimeError(f"Only {len(completed)} completed D1 bars are available")
        session_rows = completed.tail(args.sessions)
        test_start = pd.Timestamp(session_rows.iloc[0]["time"])
        test_end = min(pd.Timestamp(now), pd.Timestamp(h1.iloc[-1]["time"]) + pd.Timedelta(hours=1))

        real_yield = _fred(cache, "DFII10")
        dollar = _fred(cache, "DTWEXBGS")
        real_yield["DFII10_ma20"] = real_yield["DFII10"].rolling(20).mean()
        dollar["DTWEXBGS_ma50"] = dollar["DTWEXBGS"].rolling(50).mean()
        d1 = _merge_available(d1, real_yield)
        d1 = _merge_available(d1, dollar)
        d1 = _merge_available(d1, _cot(cache, now.year - 3, now.year))
        d1 = _merge_available(d1, _gld(cache))

        silver_daily = silver[["time", "close"]].rename(columns={"close": "silver_close"})
        d1 = pd.merge_asof(d1.sort_values("time"), silver_daily.sort_values("time"), on="time", direction="backward")
        d1["gsr"] = d1["close"] / d1["silver_close"]
        gsr_mean = d1["gsr"].rolling(252, min_periods=126).mean()
        gsr_std = d1["gsr"].rolling(252, min_periods=126).std(ddof=0)
        d1["gsr_z"] = (d1["gsr"] - gsr_mean) / gsr_std.replace(0.0, np.nan)

        candidates = _daily_candidates(d1, test_start, test_end)
        candidates["trend_pullback_h4"] = _h4_candidates(h4, d1, test_start, test_end)
        h1_test = h1[(h1["time"] >= test_start) & (h1["time"] <= test_end)].reset_index(drop=True)
        tick = mt5.symbol_info_tick(xau)
        spread = max(0.0, float(tick.ask) - float(tick.bid)) if tick else 0.0

        enabled_strategies = [key.strip() for key in args.strategies.split(",") if key.strip()]
        unknown = sorted(set(enabled_strategies) - set(STRATEGY_NAMES))
        if unknown:
            raise ValueError(f"Unknown strategies: {', '.join(unknown)}")
        results: dict[str, Any] = {}
        for key in enabled_strategies:
            stats = _simulate_strategy(
                xau,
                h1_test,
                candidates[key],
                args.lot,
                spread,
                args.max_hold_days,
                max_open_positions=max(0, args.max_open_per_strategy),
            )
            results[key] = {"name": STRATEGY_NAMES[key], **stats}

        portfolio_trades = [
            {"strategy": key, **trade}
            for key, row in results.items()
            for trade in row["trades"]
        ]
        portfolio_trades.sort(key=lambda trade: (trade["exit_time"], trade["strategy"]))
        combined_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        for trade in portfolio_trades:
            pnl = float(trade["pnl"])
            combined_pnl += pnl
            gross_profit += max(0.0, pnl)
            gross_loss += abs(min(0.0, pnl))
        closed_by_time: dict[str, float] = {}
        for trade in portfolio_trades:
            closed_by_time[trade["exit_time"]] = closed_by_time.get(trade["exit_time"], 0.0) + float(trade["pnl"])
        closed_equity = combined_peak = combined_max_dd = 0.0
        for _, pnl in sorted(closed_by_time.items()):
            closed_equity += pnl
            combined_peak = max(combined_peak, closed_equity)
            combined_max_dd = max(combined_max_dd, combined_peak - closed_equity)
        event_rows = []
        for trade in portfolio_trades:
            event_rows.extend(((trade["entry_time"], 1), (trade["exit_time"], -1)))
        active = max_concurrent = 0
        for _, change in sorted(event_rows, key=lambda event: (event[0], event[1])):
            active += change
            max_concurrent = max(max_concurrent, active)
        combined_wins = sum(float(trade["pnl"]) > 0.005 for trade in portfolio_trades)
        output = {
            "generated_utc": now.isoformat(),
            "symbol": xau,
            "silver_symbol": xag,
            "sessions": args.sessions,
            "session_start_utc": test_start.isoformat(),
            "session_end_utc": test_end.isoformat(),
            "lot_per_strategy": args.lot,
            "enabled_strategies": enabled_strategies,
            "max_open_per_strategy": args.max_open_per_strategy,
            "spread_snapshot_price": round(spread, 5),
            "execution": {
                "entry": "first H1 open after a completed D1/H4 signal candle",
                "initial_stop": "max(3 USD, 0.45 D1 ATR or 1.5 H4 ATR)",
                "target": "2R",
                "management": "BE after 1R; 1R trailing distance after 1.5R; changes active next H1 bar",
                "ambiguity": "SL before TP when both occur in one H1 bar",
                "overlap": (
                    "unlimited concurrent positions"
                    if args.max_open_per_strategy <= 0
                    else f"maximum {args.max_open_per_strategy} open positions per strategy"
                ),
                "cost": "current MT5 XAU spread charged once per trade; swap omitted",
            },
            "data_notes": {
                "macro": "FRED DFII10 and DTWEXBGS, observations delayed one UTC day",
                "cot": "CFTC disaggregated COMEX Gold; Tuesday report usable Friday",
                "etf": "SPDR GLD tonnes are a large-ETF proxy, not aggregate global ETF flow",
                "gsr": "result is the XAU leg only, not a market-neutral two-leg spread",
            },
            "combined_portfolio": {
                "executed_setups": len(portfolio_trades),
                "wins": combined_wins,
                "win_rate_pct": round(100.0 * combined_wins / max(1, len(portfolio_trades)), 2),
                "net_pnl_usd": round(combined_pnl, 2),
                "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
                "max_closed_drawdown_usd": round(combined_max_dd, 2),
                "max_concurrent_positions": max_concurrent,
                "warning": "Strategies are correlated and can place identical trades; this is a simultaneous-module sum, not diversification.",
            },
            "results": results,
        }
        output_path = root / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")

        table = []
        for key, row in results.items():
            table.append(
                {
                    "strategy": row["name"],
                    "signals": row["raw_signals"],
                    "setups": row["executed_setups"],
                    "win_rate": row["win_rate_pct"],
                    "pnl": row["net_pnl_usd"],
                    "max_dd": row["max_closed_drawdown_usd"],
                    "pf": row["profit_factor"],
                }
            )
        print(pd.DataFrame(table).to_string(index=False))
        print(f"\nCombined simultaneous-module PnL: {combined_pnl:.2f} USD")
        print(f"Report: {output_path}")
    finally:
        shutdown()


if __name__ == "__main__":
    main()
