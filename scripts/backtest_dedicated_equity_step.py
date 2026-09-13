from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown, symbol_info
from app.risk import normalize_volume


ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    ("NAS100", "dual_thrust_specialist"): "nas100_dual_thrust",
    ("DJ30", "roc_acceleration_specialist"): "dj30_roc",
}


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stats(trades: list[dict], start_equity: float) -> dict:
    ordered = sorted(trades, key=lambda row: row["exit_time"])
    profits = [float(row["dynamic_profit"]) for row in ordered]
    wins = [value for value in profits if value > 0]
    losses = [value for value in profits if value < 0]
    balance = float(start_equity)
    peak = balance
    max_dd = 0.0
    max_dd_pct = 0.0
    for profit in profits:
        balance += profit
        peak = max(peak, balance)
        drawdown = peak - balance
        max_dd = max(max_dd, drawdown)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, 100.0 * drawdown / peak)
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
        "max_closed_drawdown": round(max_dd, 2),
        "max_closed_drawdown_pct": round(max_dd_pct, 2),
    }


def _rate_frames(symbols: set[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, start, end)
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"No M5 rates for {symbol}: {mt5.last_error()}")
        frame = pd.DataFrame(raw)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        frames[symbol] = frame.sort_values("time").reset_index(drop=True)
    return frames


def _mark_price(frame: pd.DataFrame, timestamp: datetime, side: str, point: float) -> float:
    stamp = pd.Timestamp(timestamp)
    index = int(frame["time"].searchsorted(stamp, side="right")) - 1
    index = max(0, min(index, len(frame) - 1))
    row = frame.iloc[index]
    bid = float(row["open"])
    spread = max(point, float(row.get("spread", 0.0) or 0.0) * point)
    return bid if side == "buy" else bid + spread


def _calc_profit(symbol: str, side: str, volume: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, volume, entry, exit_price)
    if value is not None:
        return float(value)
    direction = 1.0 if side == "buy" else -1.0
    return direction * (exit_price - entry) * volume


def _requested_lot(
    equity: float,
    base_equity: float,
    base_lot: float,
    step_usd: float,
    step_lot: float,
) -> tuple[float, int]:
    steps = max(0, math.floor((max(0.0, equity) - base_equity) / step_usd))
    return base_lot + steps * step_lot, steps


def main() -> int:
    parser = argparse.ArgumentParser(description="Chronological equity-step backtest for dedicated index teams")
    parser.add_argument(
        "--raw-report",
        default="data_vantage/nas100_dualthrust_dj30roc_raw_60sessions_20260824.json",
    )
    parser.add_argument("--profile", default=".env.vantage")
    parser.add_argument("--start-equity", type=float, default=1000.0)
    parser.add_argument("--base-equity", type=float, default=1000.0)
    parser.add_argument("--base-lot", type=float, default=0.10)
    parser.add_argument("--step-usd", type=float, default=300.0)
    parser.add_argument("--step-lot", type=float, default=0.01)
    parser.add_argument("--max-lot", type=float, default=100.0)
    parser.add_argument(
        "--output",
        default="data_vantage/nas100_dualthrust_dj30roc_equity_step_60sessions_20260824.json",
    )
    args = parser.parse_args()

    raw_path = ROOT / args.raw_report
    raw_report = json.loads(raw_path.read_text(encoding="utf-8"))
    templates = []
    for trade in raw_report.get("trades", []):
        preferred = str(trade.get("preferred_symbol", ""))
        strategy = str(trade.get("strategy", ""))
        module = TARGETS.get((preferred, strategy))
        if not module:
            continue
        row = dict(trade)
        row["module"] = module
        row["entry_dt"] = _dt(row["entry_time"])
        row["exit_dt"] = _dt(row["exit_time"])
        templates.append(row)
    if not templates:
        raise RuntimeError("Raw report contains no dedicated target trades")

    load_dotenv(ROOT / args.profile, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        resolved = {preferred: ensure_symbol(preferred) for preferred, _ in TARGETS}
        symbols = {resolved[preferred] for preferred, _ in TARGETS}
        earliest = min(row["entry_dt"] for row in templates) - timedelta(days=1)
        latest = max(row["exit_dt"] for row in templates) + timedelta(days=1)
        frames = _rate_frames(symbols, earliest, latest)
        infos = {symbol: symbol_info(symbol) for symbol in symbols}

        templates.sort(key=lambda row: (row["entry_dt"], row["module"]))
        groups: dict[datetime, list[dict]] = defaultdict(list)
        for row in templates:
            groups[row["entry_dt"]].append(row)

        balance = float(args.start_equity)
        open_trades: list[dict] = []
        completed: list[dict] = []
        lot_path: list[dict] = []
        max_concurrent = 0
        max_margin_used = 0.0
        margin_rejections = 0

        def settle(until: datetime) -> None:
            nonlocal balance, open_trades
            closing = sorted((row for row in open_trades if row["exit_dt"] <= until), key=lambda row: row["exit_dt"])
            for row in closing:
                balance += float(row["dynamic_profit"])
                completed.append(row)
                open_trades.remove(row)

        for entry_time in sorted(groups):
            settle(entry_time)
            floating = 0.0
            margin_used = 0.0
            for active in open_trades:
                symbol = str(active["symbol"])
                point = float(getattr(infos[symbol], "point", 0.01) or 0.01)
                mark = _mark_price(frames[symbol], entry_time, str(active["side"]), point)
                floating += _calc_profit(symbol, str(active["side"]), float(active["dynamic_lot"]), float(active["entry"]), mark)
                order_type = mt5.ORDER_TYPE_BUY if active["side"] == "buy" else mt5.ORDER_TYPE_SELL
                margin = mt5.order_calc_margin(order_type, symbol, float(active["dynamic_lot"]), mark)
                margin_used += max(0.0, float(margin or 0.0))
            equity = balance + floating
            requested, steps = _requested_lot(
                equity,
                float(args.base_equity),
                float(args.base_lot),
                float(args.step_usd),
                float(args.step_lot),
            )

            group_margin = 0.0
            candidates = []
            for template in groups[entry_time]:
                symbol = str(template["symbol"])
                lot = normalize_volume(symbol, requested, float(getattr(infos[symbol], "volume_min", 0.01) or 0.01), float(args.max_lot))
                order_type = mt5.ORDER_TYPE_BUY if template["side"] == "buy" else mt5.ORDER_TYPE_SELL
                margin = max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, float(template["entry"])) or 0.0))
                candidates.append((template, lot, margin))
                group_margin += margin

            if margin_used + group_margin > max(0.0, equity):
                margin_rejections += len(candidates)
                continue

            for template, lot, margin in candidates:
                base_volume = float(template["volume"])
                dynamic_profit = float(template["profit"]) * lot / base_volume
                active = dict(template)
                active["dynamic_lot"] = round(lot, 2)
                active["dynamic_profit"] = round(dynamic_profit, 8)
                active["balance_at_entry"] = round(balance, 2)
                active["equity_at_entry"] = round(equity, 2)
                active["equity_steps"] = steps
                active["margin_at_entry"] = round(margin, 2)
                open_trades.append(active)
                lot_path.append(
                    {
                        "entry_time": active["entry_time"],
                        "module": active["module"],
                        "equity": round(equity, 2),
                        "steps": steps,
                        "lot": round(lot, 2),
                    }
                )
            max_concurrent = max(max_concurrent, len(open_trades))
            max_margin_used = max(max_margin_used, margin_used + group_margin)

        settle(datetime.max.replace(tzinfo=UTC))
    finally:
        shutdown()

    by_module: dict[str, list[dict]] = defaultdict(list)
    for row in completed:
        by_module[str(row["module"])].append(row)
    daily: dict[str, float] = defaultdict(float)
    for row in completed:
        daily[row["exit_dt"].date().isoformat()] += float(row["dynamic_profit"])

    portfolio_stats = _stats(completed, float(args.start_equity))
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source_report": str(raw_path.resolve()),
        "sessions": int(raw_report.get("sessions", 60)),
        "period": {
            "first_entry": min(row["entry_time"] for row in completed),
            "last_exit": max(row["exit_time"] for row in completed),
        },
        "method": (
            "Signals and exits from conservative M5 SL-first backtest; portfolio replayed chronologically. "
            "Lot uses account equity at entry, including marked-to-market open dedicated positions."
        ),
        "lot_rule": {
            "start_equity": float(args.start_equity),
            "base_equity": float(args.base_equity),
            "base_lot": float(args.base_lot),
            "step_usd": float(args.step_usd),
            "step_lot": float(args.step_lot),
            "formula": "base_lot + floor(max(0, equity - base_equity) / step_usd) * step_lot",
        },
        "portfolio": {
            **portfolio_stats,
            "start_equity": float(args.start_equity),
            "end_balance": round(float(args.start_equity) + float(portfolio_stats["pnl"]), 2),
            "return_pct": round(100.0 * float(portfolio_stats["pnl"]) / float(args.start_equity), 2),
            "max_concurrent": max_concurrent,
            "max_margin_used": round(max_margin_used, 2),
            "margin_rejections": margin_rejections,
            "minimum_lot": min(float(row["dynamic_lot"]) for row in completed),
            "maximum_lot": max(float(row["dynamic_lot"]) for row in completed),
            "next_lot": round(
                _requested_lot(
                    float(args.start_equity) + float(portfolio_stats["pnl"]),
                    float(args.base_equity),
                    float(args.base_lot),
                    float(args.step_usd),
                    float(args.step_lot),
                )[0],
                2,
            ),
        },
        "modules": {key: _stats(value, float(args.start_equity)) for key, value in sorted(by_module.items())},
        "daily_pnl": {key: round(value, 2) for key, value in sorted(daily.items())},
        "lot_changes": [
            row
            for index, row in enumerate(lot_path)
            if index == 0 or row["lot"] != lot_path[index - 1]["lot"]
        ],
        "trades": [
            {key: value for key, value in row.items() if key not in {"entry_dt", "exit_dt"}}
            for row in sorted(completed, key=lambda item: item["entry_dt"])
        ],
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "trades"}, indent=2, ensure_ascii=False))
    print(f"REPORT={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
