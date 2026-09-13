from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_teams import (
    _agent_votes,
    _candidate_votes,
    _dedicated_strategy_parameters,
    _default_learning_state,
    supervise,
)
from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown, symbol_info


DEFAULT_SYMBOLS = (
    "XAUUSD,NAS100,DJ30,SP500,EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,BTCUSD,"
    "ETHUSD,USDCHF,NZDUSD,EURJPY,GBPJPY,EURGBP,AUDJPY,NZDJPY,CADJPY,CHFJPY,"
    "EURAUD,EURCAD,EURCHF,EURNZD,GBPAUD,GBPCAD,GBPCHF,GBPNZD,XAGUSD,GER40"
)


def _rates(symbol: str, timeframe: int, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, timeframe, start, end)
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"No rates for {symbol}: {mt5.last_error()}")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _frame_window(frame: pd.DataFrame, timestamp: pd.Timestamp, bars: int = 280) -> pd.DataFrame:
    position = int(frame["time"].searchsorted(timestamp, side="right"))
    return frame.iloc[max(0, position - bars) : position]


def _strategy_levels(strategy: str) -> tuple[float, float, int]:
    return _dedicated_strategy_parameters(strategy)


def _simulate_trade(
    symbol: str,
    bars: pd.DataFrame,
    entry_index: int,
    side: str,
    atr_value: float,
    spread_price: float,
    strategy: str,
    volume: float,
) -> dict:
    stop_atr, reward_risk, max_bars = _strategy_levels(strategy)
    raw_open = float(bars.iloc[entry_index]["open"])
    entry = raw_open + spread_price if side == "buy" else raw_open
    stop_distance = max(atr_value * stop_atr, spread_price * 4.0)
    target_distance = stop_distance * reward_risk
    sl = entry - stop_distance if side == "buy" else entry + stop_distance
    tp = entry + target_distance if side == "buy" else entry - target_distance
    exit_index = min(len(bars) - 1, entry_index + max_bars)
    exit_price = float(bars.iloc[exit_index]["close"])
    reason = "timeout"

    for index in range(entry_index, exit_index + 1):
        row = bars.iloc[index]
        high = float(row["high"])
        low = float(row["low"])
        if side == "sell":
            high += spread_price
            low += spread_price
        hit_sl = low <= sl if side == "buy" else high >= sl
        hit_tp = high >= tp if side == "buy" else low <= tp
        if hit_sl:
            exit_index, exit_price, reason = index, sl, "sl"
            break
        if hit_tp:
            exit_index, exit_price, reason = index, tp, "tp"
            break

    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    profit = mt5.order_calc_profit(order_type, symbol, volume, entry, exit_price)
    if profit is None:
        direction = 1.0 if side == "buy" else -1.0
        profit = direction * (exit_price - entry) * volume
    return {
        "entry_index": entry_index,
        "exit_index": exit_index,
        "entry_time": pd.Timestamp(bars.iloc[entry_index]["time"]).isoformat(),
        "exit_time": pd.Timestamp(bars.iloc[exit_index]["time"]).isoformat(),
        "side": side,
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "exit": round(exit_price, 8),
        "reason": reason,
        "profit": round(float(profit), 2),
        "volume": volume,
    }


def _statistics(trades: list[dict]) -> dict:
    ordered = sorted(trades, key=lambda item: item["exit_time"])
    profits = [float(item["profit"]) for item in ordered]
    wins = [value for value in profits if value > 0]
    losses = [value for value in profits if value < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in profits:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(ordered),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / max(1, len(ordered)), 2),
        "pnl": round(sum(profits), 2),
        "average_win": round(gross_profit / max(1, len(wins)), 2),
        "average_loss": round(sum(losses) / max(1, len(losses)), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown": round(max_drawdown, 2),
    }


def _backtest_symbol(
    preferred: str,
    start: datetime,
    end: datetime,
    sessions: int,
    strategy_filter: set[str] | None = None,
) -> tuple[str, list[dict]]:
    symbol = ensure_symbol(preferred)
    info = symbol_info(symbol)
    volume = float(getattr(info, "volume_min", 0.01) or 0.01)
    point = float(getattr(info, "point", 0.00001) or 0.00001)
    m5 = _rates(symbol, mt5.TIMEFRAME_M5, start, end)
    m15 = _rates(symbol, mt5.TIMEFRAME_M15, start, end)
    h1 = _rates(symbol, mt5.TIMEFRAME_H1, start, end)
    trading_dates = list(dict.fromkeys(pd.to_datetime(m5["time"], utc=True).dt.date.tolist()))
    selected_dates = set(trading_dates[-sessions:])
    first_date = min(selected_dates)
    learning = _default_learning_state()
    next_available: dict[str, int] = defaultdict(int)
    trades: list[dict] = []

    for index in range(260, len(m5) - 1, 3):
        signal_time = pd.Timestamp(m5.iloc[index]["time"])
        if signal_time.date() < first_date or signal_time.date() not in selected_dates:
            continue
        entry_window = m5.iloc[max(0, index - 279) : index + 1]
        trend_window = _frame_window(m15, signal_time)
        macro_window = _frame_window(h1, signal_time)
        if min(len(entry_window), len(trend_window), len(macro_window)) < 205:
            continue
        spread_points = float(m5.iloc[index - 1].get("spread", 0.0) or 0.0)
        spread_price = max(point, spread_points * point)
        votes = _agent_votes(entry_window, trend_window, macro_window, spread_price)
        candidate_votes = _candidate_votes(entry_window, trend_window, macro_window, learning, spread_price)
        votes.extend(candidate_votes)

        research_votes = []
        for vote in votes:
            research_vote = dict(vote)
            research_vote["status"] = "active"
            research_votes.append(research_vote)
        ensemble = supervise(research_votes, threshold=0.55, min_directional_votes=4)
        if ensemble["decision"] in {"buy", "sell"}:
            votes.append(
                {
                    "agent": "portfolio_ensemble",
                    "side": ensemble["decision"],
                    "confidence": ensemble["confidence"],
                    "status": "active",
                }
            )

        atr_value = max(float(m5.iloc[index - 1]["atr14"]), point * 10.0)
        for vote in votes:
            strategy = str(vote["agent"])
            if strategy_filter and strategy not in strategy_filter:
                continue
            side = str(vote["side"])
            confidence = float(vote.get("confidence", 0.0) or 0.0)
            if side not in {"buy", "sell"} or confidence < 0.58 or index < next_available[strategy]:
                continue
            trade = _simulate_trade(symbol, m5, index, side, atr_value, spread_price, strategy, volume)
            trade.update(
                {
                    "symbol": symbol,
                    "preferred_symbol": preferred,
                    "strategy": strategy,
                    "confidence": round(confidence, 3),
                }
            )
            trades.append(trade)
            next_available[strategy] = int(trade["exit_index"]) + 1
    return symbol, trades


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward research for the multi-instrument Agent Teams machine")
    parser.add_argument("--profile", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--strategies", default="")
    parser.add_argument("--output", default="data_vantage/market_machine_backtest_60sessions.json")
    args = parser.parse_args()

    load_dotenv(args.profile, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    end = datetime.now(UTC)
    start = end - timedelta(days=max(120, args.sessions * 2 + 30))
    all_trades: list[dict] = []
    errors: list[dict] = []
    resolved: dict[str, str] = {}
    strategy_filter = {item.strip() for item in args.strategies.split(",") if item.strip()} or None
    try:
        for preferred in [item.strip() for item in args.symbols.split(",") if item.strip()]:
            try:
                symbol, trades = _backtest_symbol(preferred, start, end, args.sessions, strategy_filter)
                resolved[preferred] = symbol
                all_trades.extend(trades)
            except Exception as exc:
                errors.append({"symbol": preferred, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        shutdown()

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for trade in all_trades:
        by_strategy[trade["strategy"]].append(trade)
        by_symbol[trade["symbol"]].append(trade)
        by_pair[f"{trade['symbol']}::{trade['strategy']}"].append(trade)
    strategy_stats = {key: _statistics(value) for key, value in sorted(by_strategy.items())}
    symbol_stats = {key: _statistics(value) for key, value in sorted(by_symbol.items())}
    pair_stats = {key: _statistics(value) for key, value in sorted(by_pair.items())}
    promoted = [
        key
        for key, stats in pair_stats.items()
        if stats["trades"] >= 20 and stats["pnl"] > 0 and (stats["profit_factor"] or 0.0) >= 1.05
    ]
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "method": "closed bars, next M5 open, historical spread, conservative SL-first ambiguity",
        "sessions": args.sessions,
        "profile": args.profile,
        "strategy_filter": sorted(strategy_filter or []),
        "resolved_symbols": resolved,
        "portfolio": _statistics(all_trades),
        "strategies": strategy_stats,
        "symbols": symbol_stats,
        "symbol_strategy_matrix": pair_stats,
        "research_promotion_candidates": promoted,
        "errors": errors,
        "trades": sorted(all_trades, key=lambda item: item["entry_time"]),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    summary = {
        key: value
        for key, value in report.items()
        if key not in {"trades", "symbol_strategy_matrix"}
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"REPORT={output.resolve()}")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
