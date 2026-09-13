from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown, symbol_info
from app.risk import normalize_volume


TARGET_AGENT_PAIRS = {
    ("NAS100", "dual_thrust_specialist"): ("agent_nas100_dual_thrust", "NAS100"),
    ("DJ30", "roc_acceleration_specialist"): ("agent_dj30_roc", "DJ30"),
}


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads((ROOT / path).read_text(encoding="utf-8-sig"))


def _dt(value: str, broker_offset_hours: int = 2) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=broker_offset_hours)))
    return parsed.astimezone(UTC)


def _event(source: str, module: str, symbol: str, opened: str, closed: str, side: str, entry: float, pnl_001: float, **extra: Any) -> dict[str, Any]:
    return {
        "source": source,
        "module": module,
        "symbol": symbol,
        "opened": _dt(opened),
        "closed": _dt(closed),
        "side": str(side),
        "entry": float(entry),
        "pnl_001": float(pnl_001),
        **extra,
    }


def _events(args: argparse.Namespace, xau_symbol: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    full = _load(args.phoenix_full)
    for row in full.get("trades", []):
        target = int(row.get("target_index", 1) or 1)
        events.append(_event("phoenix_full", f"Phoenix TP{target}", xau_symbol, row["entry_time"], row["exit_time"], row["side"], row["entry"], row["pnl_001"], sl=float(row.get("sl", 0.0) or 0.0)))
    ranges = _load(args.phoenix_range)
    for row in ranges.get("trades", []):
        events.append(_event("phoenix_range", "Phoenix range", xau_symbol, row["entry_time"], row["exit_time"], row["side"], row["entry"], row["pnl_001"], sl=float(row.get("sl", 0.0) or 0.0)))
    direction = _load(args.phoenix_direction)
    for row in direction.get("trades", []):
        events.append(_event("phoenix_direction", "Phoenix direction pullback", xau_symbol, row["entry_time"], row["exit_time"], row["side"], row["entry"], row["pnl_001"], sl=float(row.get("sl", 0.0) or 0.0)))
    for source, module, path in (
        ("scalper_ind01", "IND-BB-KELT-MACD", args.scalper_ind01),
        ("scalper_bbrcl", "IND-BB-MACD-RCL", args.scalper_bbrcl),
    ):
        report = _load(path)
        for row in report.get("trades", []):
            original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
            net_001 = float(row.get("profit", 0.0) or 0.0) / (original_lot / 0.01)
            events.append(_event(source, module, xau_symbol, row["opened"], row["closed"], row["side"], row["entry"], net_001))
    long_term = _load(args.long_term)
    for key, strategy in long_term.get("results", {}).items():
        for row in strategy.get("trades", []):
            events.append(_event("long_term", f"Long term: {key}", xau_symbol, row["entry_time"], row["exit_time"], row["side"], row["entry"], row["pnl"]))
    autonomous = _load(args.autonomous)
    for row in autonomous.get("trades", []):
        events.append(_event("autonomous_core", f"Autonomous: {row['strategy']}", xau_symbol, row["opened"], row["closed"], row["side"], row["entry"], row["profit_001"], sl=float(row.get("initial_sl", 0.0) or 0.0)))
    agents = _load(args.agents_raw)
    for row in agents.get("trades", []):
        target = TARGET_AGENT_PAIRS.get((str(row.get("preferred_symbol", "")), str(row.get("strategy", ""))))
        if not target:
            continue
        module, symbol = target
        base_volume = max(0.01, float(row.get("volume", 0.01) or 0.01))
        pnl_001 = float(row.get("profit", 0.0) or 0.0) * 0.01 / base_volume
        events.append(_event("agent_teams", module, symbol, row["entry_time"], row["exit_time"], row["side"], row["entry"], pnl_001, sl=float(row.get("sl", 0.0) or 0.0)))
    include_sources = {
        value.strip()
        for value in str(getattr(args, "include_sources", "") or "").split(",")
        if value.strip()
    }
    if include_sources:
        events = [event for event in events if event["source"] in include_sources]
    if getattr(args, "from_date", ""):
        cutoff = datetime.fromisoformat(str(args.from_date)).date()
        events = [event for event in events if event["opened"].date() >= cutoff]
    if getattr(args, "to_date", ""):
        cutoff = datetime.fromisoformat(str(args.to_date)).date()
        events = [event for event in events if event["opened"].date() <= cutoff]
    return sorted(events, key=lambda row: (row["opened"], row["module"], row["closed"]))


def _frames(symbols: set[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    output = {}
    for symbol in symbols:
        timeframe = mt5.TIMEFRAME_M1 if "XAU" in symbol.upper() else mt5.TIMEFRAME_M5
        raw = mt5.copy_rates_range(symbol, timeframe, start, end)
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"No rates for {symbol}: {mt5.last_error()}")
        frame = pd.DataFrame(raw)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        output[symbol] = frame.sort_values("time").reset_index(drop=True)
    return output


def _mark(frame: pd.DataFrame, moment: datetime, side: str, point: float) -> float:
    index = int(frame["time"].searchsorted(pd.Timestamp(moment), side="right")) - 1
    index = max(0, min(index, len(frame) - 1))
    row = frame.iloc[index]
    bid = float(row["open"])
    spread = max(point, float(row.get("spread", 0.0) or 0.0) * point)
    return bid if side == "buy" else bid + spread


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price)
    if value is not None:
        return float(value)
    return (exit_price - entry) * lot * (1.0 if side == "buy" else -1.0)


def _lot(
    event: dict[str, Any],
    balance: float,
    equity: float,
    symbol: str,
    cfg: Any,
    fixed_control: bool,
    phoenix_max_lot: float = 0.0,
    phoenix_base_balance: float = -1.0,
    phoenix_base_lot: float = 0.0,
    phoenix_step_usd: float = 0.0,
    phoenix_step_lot: float = -1.0,
) -> float:
    if fixed_control:
        raw = 0.01
    elif event["source"] in {"phoenix_full", "phoenix_direction"}:
        base_balance = max(0.0, phoenix_base_balance) if phoenix_base_balance >= 0.0 else max(0.0, float(os.getenv("PHOENIX_LOT_BASE_BALANCE_USD", "1000") or 1000))
        base_lot = max(0.01, phoenix_base_lot) if phoenix_base_lot > 0.0 else max(0.01, float(os.getenv("PHOENIX_LOT_BASE_PER_POSITION", "0.10") or 0.10))
        step_usd = max(1.0, phoenix_step_usd) if phoenix_step_usd > 0.0 else max(1.0, float(os.getenv("PHOENIX_LOT_BALANCE_STEP_USD", "300") or 300))
        step_lot = max(0.0, phoenix_step_lot) if phoenix_step_lot >= 0.0 else max(0.0, float(os.getenv("PHOENIX_LOT_STEP_ADD", "0.01") or 0.01))
        raw = base_lot + max(0, math.floor((balance - base_balance) / step_usd)) * step_lot
        configured_cap = max(0.01, float(os.getenv("PHOENIX_LOT_MAX_PER_POSITION", "999") or 999))
        raw = min(raw, phoenix_max_lot if phoenix_max_lot > 0.0 else configured_cap)
    elif event["source"] == "phoenix_range":
        raw = float(os.getenv("PHOENIX_RANGE_TRIGGER_FIXED_LOT", "0.01") or 0.01)
    elif event["source"] == "scalper_ind01":
        raw = 0.01
    elif event["source"] == "scalper_bbrcl":
        raw = 0.01 + max(0, math.floor((balance - 1000.0) / 200.0)) * 0.01
    elif event["source"] == "long_term":
        raw = 0.01
    elif event["source"] == "agent_teams":
        raw = 0.10 + max(0, math.floor((equity - 1000.0) / 300.0)) * 0.01
    elif event["source"] == "autonomous_core":
        sl = float(event.get("sl", 0.0) or 0.0)
        loss_per_lot = abs(_profit(symbol, event["side"], 1.0, float(event["entry"]), sl)) if sl > 0 else 0.0
        raw = (max(0.0, equity) * float(cfg.risk_per_trade_pct) / 100.0 / loss_per_lot) if loss_per_lot > 0 else float(cfg.min_lot)
    else:
        raw = 0.01
    return normalize_volume(symbol, raw, float(cfg.min_lot), max(float(cfg.max_lot), 100.0))


def _simulate(
    events: list[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
    infos: dict[str, Any],
    cfg: Any,
    start_balance: float,
    fixed_control: bool,
    phoenix_max_lot: float = 0.0,
    phoenix_base_balance: float = -1.0,
    phoenix_base_lot: float = 0.0,
    phoenix_step_usd: float = 0.0,
    phoenix_step_lot: float = -1.0,
) -> dict[str, Any]:
    balance = float(start_balance)
    peak_balance = balance
    min_balance = balance
    max_closed_dd = 0.0
    peak_equity = balance
    min_observed_equity = balance
    max_observed_equity_dd = 0.0
    open_trades: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    margin_skips = 0
    max_margin = 0.0
    max_positions = 0
    by_module: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    daily: dict[str, float] = defaultdict(float)
    lot_ranges: dict[str, list[float]] = defaultdict(list)

    def settle(moment: datetime) -> None:
        nonlocal balance, peak_balance, min_balance, max_closed_dd
        closing = sorted((row for row in open_trades if row["closed"] <= moment), key=lambda row: row["closed"])
        for row in closing:
            pnl = float(row["pnl"])
            balance += pnl
            peak_balance = max(peak_balance, balance)
            min_balance = min(min_balance, balance)
            max_closed_dd = max(max_closed_dd, peak_balance - balance)
            bucket = by_module[row["module"]]
            bucket["pnl"] += pnl
            bucket["gross_win"] += max(0.0, pnl)
            bucket["gross_loss"] += min(0.0, pnl)
            bucket["wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"] += 1
            daily[row["closed"].date().isoformat()] += pnl
            completed.append(row)
            open_trades.remove(row)

    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["opened"]].append(event)
    for moment in sorted(grouped):
        settle(moment)
        floating = 0.0
        used_margin = 0.0
        for active in open_trades:
            symbol = active["symbol"]
            point = float(getattr(infos[symbol], "point", 0.01) or 0.01)
            mark = _mark(frames[symbol], moment, active["side"], point)
            floating += _profit(symbol, active["side"], active["lot"], active["entry"], mark)
            order_type = mt5.ORDER_TYPE_BUY if active["side"] == "buy" else mt5.ORDER_TYPE_SELL
            used_margin += max(0.0, float(mt5.order_calc_margin(order_type, symbol, active["lot"], mark) or 0.0))
        equity = balance + floating
        peak_equity = max(peak_equity, equity)
        min_observed_equity = min(min_observed_equity, equity)
        max_observed_equity_dd = max(max_observed_equity_dd, peak_equity - equity)
        for event in grouped[moment]:
            symbol = event["symbol"]
            lot = _lot(
                event,
                balance,
                equity,
                symbol,
                cfg,
                fixed_control,
                phoenix_max_lot,
                phoenix_base_balance,
                phoenix_base_lot,
                phoenix_step_usd,
                phoenix_step_lot,
            )
            order_type = mt5.ORDER_TYPE_BUY if event["side"] == "buy" else mt5.ORDER_TYPE_SELL
            margin = max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, event["entry"]) or 0.0))
            if margin > max(0.0, equity - used_margin) + 0.01:
                margin_skips += 1
                continue
            active = {**event, "lot": lot, "margin": margin, "pnl": float(event["pnl_001"]) * lot / 0.01, "balance_at_entry": balance, "equity_at_entry": equity}
            open_trades.append(active)
            used_margin += margin
            lot_ranges[event["module"]].append(lot)
        max_margin = max(max_margin, used_margin)
        max_positions = max(max_positions, len(open_trades))
    settle(datetime.max.replace(tzinfo=UTC))

    def bucket(values: dict[str, float]) -> dict[str, Any]:
        wins, losses, flat = int(values["wins"]), int(values["losses"]), int(values["flat"])
        gross_loss = abs(float(values["gross_loss"]))
        return {"positions": wins + losses + flat, "wins": wins, "losses": losses, "flat": flat, "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2), "pnl": round(values["pnl"], 2), "profit_factor": round(values["gross_win"] / gross_loss, 3) if gross_loss else None}

    total_values: dict[str, float] = defaultdict(float)
    for values in by_module.values():
        for key, value in values.items():
            total_values[key] += value
    formatted_daily = {key: round(value, 2) for key, value in sorted(daily.items())}
    positive_days = sum(value > 0 for value in formatted_daily.values())
    negative_days = sum(value < 0 for value in formatted_daily.values())
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "peak_closed_balance": round(peak_balance, 2),
        "minimum_closed_balance": round(min_balance, 2),
        "peak_observed_entry_equity": round(peak_equity, 2),
        "minimum_observed_entry_equity": round(min_observed_equity, 2),
        "max_closed_drawdown_usd": round(max_closed_dd, 2),
        "max_closed_drawdown_pct_start": round(100.0 * max_closed_dd / start_balance, 2),
        "max_observed_entry_equity_drawdown_usd": round(max_observed_equity_dd, 2),
        "candidate_positions": len(events),
        "accepted_positions": len(completed),
        "margin_skips": margin_skips,
        "max_concurrent_positions": max_positions,
        "max_used_margin": round(max_margin, 2),
        "overall": bucket(total_values),
        "positive_days": positive_days,
        "negative_days": negative_days,
        "daily_win_rate_pct": round(100.0 * positive_days / max(1, positive_days + negative_days), 2),
        "by_module": {key: bucket(value) for key, value in sorted(by_module.items())},
        "lot_ranges": {key: {"min": min(values), "max": max(values)} for key, values in sorted(lot_ranges.items()) if values},
        "daily_pnl": formatted_daily,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="All current bot modules on one chronological 60-session portfolio")
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--signal-env", default=".env.vantage.signal")
    parser.add_argument("--agent-env", default=".env.vantage.agent_teams")
    parser.add_argument("--phoenix-full", required=True)
    parser.add_argument("--phoenix-range", required=True)
    parser.add_argument("--phoenix-direction", required=True)
    parser.add_argument("--scalper-ind01", required=True)
    parser.add_argument("--scalper-bbrcl", required=True)
    parser.add_argument("--long-term", required=True)
    parser.add_argument("--autonomous", required=True)
    parser.add_argument("--agents-raw", required=True)
    parser.add_argument(
        "--include-sources",
        default="",
        help="Optional comma-separated source filter for portfolio selection",
    )
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--from-date", default="", help="Optional inclusive event date, YYYY-MM-DD")
    parser.add_argument("--to-date", default="", help="Optional inclusive event date, YYYY-MM-DD")
    parser.add_argument(
        "--phoenix-max-lot",
        type=float,
        default=0.0,
        help="Optional simulation-only cap per Phoenix Full/Direction position",
    )
    parser.add_argument("--phoenix-base-balance", type=float, default=-1.0)
    parser.add_argument("--phoenix-base-lot", type=float, default=0.0)
    parser.add_argument("--phoenix-step-usd", type=float, default=0.0)
    parser.add_argument("--phoenix-step-lot", type=float, default=-1.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    for path in (args.env, args.signal_env, args.agent_env):
        load_dotenv(ROOT / path, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        xau = ensure_symbol(cfg.symbol)
        resolved = {"NAS100": ensure_symbol("NAS100"), "DJ30": ensure_symbol("DJ30")}
        events = _events(args, xau)
        for event in events:
            event["symbol"] = resolved.get(event["symbol"], event["symbol"])
        symbols = {event["symbol"] for event in events}
        start = min(event["opened"] for event in events) - timedelta(days=1)
        end = max(event["closed"] for event in events) + timedelta(days=1)
        frames = _frames(symbols, start, end)
        infos = {symbol: symbol_info(symbol) for symbol in symbols}
        dynamic = _simulate(
            events,
            frames,
            infos,
            cfg,
            float(args.start_balance),
            False,
            max(0.0, args.phoenix_max_lot),
            float(args.phoenix_base_balance),
            max(0.0, float(args.phoenix_base_lot)),
            max(0.0, float(args.phoenix_step_usd)),
            float(args.phoenix_step_lot),
        )
        fixed = _simulate(events, frames, infos, cfg, float(args.start_balance), True)
    finally:
        shutdown()
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "sessions": len({event["opened"].date() for event in events}),
        "range_utc": {"start": min(event["opened"] for event in events).isoformat(), "end": max(event["closed"] for event in events).isoformat()},
        "method": "All enabled execution modules merged chronologically; historical spread/costs inherited from source reports; equity-step modules use marked-to-market equity at entry; broker margin enforced.",
        "active_modules": sorted({event["module"] for event in events}),
        "dynamic_lot_rules": {
            "phoenix_full_and_direction": {
                "base_balance_usd": args.phoenix_base_balance,
                "base_lot_per_position": args.phoenix_base_lot,
                "balance_step_usd": args.phoenix_step_usd,
                "step_lot": args.phoenix_step_lot,
                "max_lot_per_position": args.phoenix_max_lot,
            },
            "phoenix_range": "fixed 0.01",
            "scalper_ind01": "fixed 0.01",
            "scalper_bbrcl": "0.01 + 0.01 per 200 USD closed profit above 1000 USD",
            "long_term": "fixed 0.01",
            "agent_nas100_and_dj30": "0.10 at 1000 USD + 0.01 per 300 USD equity",
            "autonomous_core": "0.40% equity risk from signal SL",
        },
        "dynamic": dynamic,
        "fixed_001_control": fixed,
        "limitations": [
            "This is an in-sample historical reconstruction, not a forecast or guarantee.",
            "Phoenix edits and exact tick ordering inside one minute cannot be fully reconstructed; ambiguous source tests use conservative loss-first handling.",
            "Autonomous core uses candle-close BE/trailing and omits tick-level reversal exits and broker-rounded partial closes, so its contribution has lower confidence.",
            "Observed floating-equity drawdown is sampled at new entries; true tick-level maximum floating drawdown can be larger.",
            "The generic Nasdaq ensemble produced no separately validated event stream; its two dedicated live strategies are included explicitly.",
        ],
    }
    path = ROOT / args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"output": str(path), "dynamic": {key: dynamic[key] for key in ("final_balance", "profit", "return_pct", "max_closed_drawdown_usd", "max_observed_entry_equity_drawdown_usd", "accepted_positions", "margin_skips")}, "fixed_001": {key: fixed[key] for key in ("final_balance", "profit", "return_pct", "max_closed_drawdown_usd")}, "by_module": dynamic["by_module"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
