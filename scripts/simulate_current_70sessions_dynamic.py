from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _channel_override(cfg: Any, chat_id: int) -> float | None:
    variants = {str(chat_id), str(abs(chat_id))}
    absolute = str(abs(chat_id))
    if absolute.startswith("100"):
        variants.add(absolute[3:])
    for key, value in cfg.channel_lot_sizes.items():
        raw = str(key).strip()
        if raw in variants or str(abs(int(raw))) in variants:
            return float(value)
    return None


def _session_cutoff(symbol: str, sessions: int) -> tuple[datetime, list[str], pd.DataFrame]:
    raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 99_999)
    if raw is None or len(raw) == 0:
        raise RuntimeError("No M1 rates returned")
    rates = pd.DataFrame(raw)
    rates["time"] = pd.to_datetime(rates["time"], unit="s", utc=True)
    dates = sorted({stamp.date() for stamp in rates["time"]})
    selected = dates[-max(1, sessions) :]
    cutoff = datetime.combine(selected[0], datetime.min.time(), tzinfo=UTC)
    return cutoff, [value.isoformat() for value in selected], rates


def _listener_events(report: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]]:
    spread = float(report.get("spread_price", 0.0) or 0.0)
    output = []
    for row in report.get("equity", []):
        opened = _dt(row["entry_time"])
        if opened < cutoff:
            continue
        output.append(
            {
                "source": "telegram",
                "module": "full_signal",
                "channel": str(row.get("channel") or "unknown"),
                "chat_id": int(row.get("chat_id", 0) or 0),
                "opened": opened,
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "initial_sl": float(row.get("initial_sl", 0.0) or 0.0),
                "profit_001": float(row["profit_001"]),
                "spread_per_001": abs(_profit(report["symbol"], "buy", 0.01, float(row["entry"]), float(row["entry"]) + spread)),
                "status": str(row.get("status") or ""),
            }
        )
    return output


def _scalper_events(report: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]]:
    output = []
    for row in report.get("trades", []):
        opened = _dt(row["opened"])
        if opened < cutoff:
            continue
        historical_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        output.append(
            {
                "source": "scalper",
                "module": str(row.get("setup_tag") or "XAU_SCALPER"),
                "channel": "XAU SCALPER",
                "chat_id": 0,
                "opened": opened,
                "closed": _dt(row["closed"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "initial_sl": float(row["entry"]) - 3.0 if str(row["side"]) == "buy" else float(row["entry"]) + 3.0,
                "profit_001": float(row["profit_001"]),
                "spread_per_001": float(row.get("spread_cost", 0.0) or 0.0) / (historical_lot / 0.01),
                "status": str(row.get("status") or ""),
            }
        )
    return output


def _range_events(
    report: dict[str, Any],
    rates: pd.DataFrame,
    symbol: str,
    cutoff: datetime,
    spread_price: float,
) -> list[dict[str, Any]]:
    output = []
    tp_distance, sl_distance = 2.5, 6.0
    be_trigger, be_buffer = 0.5, 0.25
    times = rates["time"]
    spread_per_001 = abs(_profit(symbol, "buy", 0.01, 4000.0, 4000.0 + spread_price))
    for pair in report.get("pairs", []):
        opened_hint = _dt(pair["range_time"])
        if opened_hint < cutoff:
            continue
        idx = int(times.searchsorted(pd.Timestamp(opened_hint).ceil("1min"), side="left"))
        if idx >= len(rates):
            continue
        entry = float(rates.iloc[idx]["open"])
        side = str(pair["side"])
        low, high = float(pair["low"]), float(pair["high"])
        if (side == "buy" and entry > high + 2.0) or (side == "sell" and entry < low - 2.0):
            continue
        tp = entry + tp_distance if side == "buy" else entry - tp_distance
        initial_sl = entry - sl_distance if side == "buy" else entry + sl_distance
        current_sl = initial_sl
        end_time = rates.iloc[idx]["time"] + pd.Timedelta(hours=6)
        end_idx = min(int(times.searchsorted(end_time, side="right")), len(rates))
        exit_idx = max(idx, end_idx - 1)
        exit_price = float(rates.iloc[exit_idx]["close"])
        status = "timeout"
        for bar_idx in range(idx, end_idx):
            bar = rates.iloc[bar_idx]
            high_price, low_price = float(bar["high"]), float(bar["low"])
            hit_sl = low_price <= current_sl if side == "buy" else high_price >= current_sl
            hit_tp = high_price >= tp if side == "buy" else low_price <= tp
            if hit_sl:
                exit_idx, exit_price = bar_idx, current_sl
                status = "loss" if current_sl == initial_sl else "protected"
                break
            if hit_tp:
                exit_idx, exit_price, status = bar_idx, tp, "win"
                break
            advance = high_price - entry if side == "buy" else entry - low_price
            if advance >= be_trigger:
                current_sl = max(current_sl, entry + be_buffer) if side == "buy" else min(current_sl, entry - be_buffer)
        output.append(
            {
                "source": "telegram",
                "module": "phoenix_early_range",
                "channel": "PHOENIX VIP - early range",
                "chat_id": -1002864291293,
                "opened": rates.iloc[idx]["time"].to_pydatetime(),
                "closed": rates.iloc[exit_idx]["time"].to_pydatetime(),
                "side": side,
                "entry": entry,
                "initial_sl": initial_sl,
                "profit_001": _profit(symbol, side, 0.01, entry, exit_price),
                "spread_per_001": spread_per_001,
                "status": status,
            }
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--listener", required=True)
    parser.add_argument("--scalper", required=True)
    parser.add_argument("--range-report", required=True)
    parser.add_argument("--sessions", type=int, default=70)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--max-leg-lot", type=float, default=0.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        cutoff, session_dates, rates = _session_cutoff(symbol, int(args.sessions))
        listener_report = json.loads(Path(args.listener).read_text(encoding="utf-8"))
        scalper_report = json.loads(Path(args.scalper).read_text(encoding="utf-8"))
        range_report = json.loads(Path(args.range_report).read_text(encoding="utf-8"))
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        spread_price = float(rates["spread"].median()) * point
        events = _listener_events(listener_report, cutoff)
        events += _scalper_events(scalper_report, cutoff)
        events += _range_events(range_report, rates, symbol, cutoff, spread_price)
        events.sort(key=lambda row: (row["opened"], row["closed"], row["source"], row["channel"]))

        start_balance = float(args.start_balance)
        balance = start_balance
        peak = balance
        max_dd = 0.0
        max_dd_pct_peak = 0.0
        min_balance = balance
        used_margin = 0.0
        open_risk = 0.0
        max_open_risk = 0.0
        max_margin = 0.0
        max_concurrent = 0
        max_total_lot = 0.0
        accepted = skipped_margin = 0
        wins = losses = flats = 0
        sequence = 0
        open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
        by_source: dict[str, float] = defaultdict(float)
        by_channel: dict[str, float] = defaultdict(float)
        by_module: dict[str, float] = defaultdict(float)
        by_channel_count: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        daily: dict[str, float] = defaultdict(float)
        lot_milestones: list[dict[str, Any]] = []
        last_steps = -1

        def close_until(moment: datetime) -> None:
            nonlocal balance, peak, max_dd, max_dd_pct_peak, min_balance, used_margin, open_risk, wins, losses, flats
            while open_heap and open_heap[0][0] <= moment:
                _, _, trade = heapq.heappop(open_heap)
                used_margin = max(0.0, used_margin - float(trade["margin"]))
                open_risk = max(0.0, open_risk - float(trade["initial_risk"]))
                pnl = float(trade["pnl"])
                balance += pnl
                peak = max(peak, balance)
                max_dd = min(max_dd, balance - peak)
                max_dd_pct_peak = max(max_dd_pct_peak, max(0.0, (peak - balance) / max(peak, 0.01) * 100.0))
                min_balance = min(min_balance, balance)
                by_source[trade["source"]] += pnl
                by_channel[trade["channel"]] += pnl
                by_module[trade["module"]] += pnl
                daily[trade["closed"].date().isoformat()] += pnl
                index = 0 if pnl > 0.005 else (1 if pnl < -0.005 else 2)
                by_channel_count[trade["channel"]][index] += 1
                if index == 0:
                    wins += 1
                elif index == 1:
                    losses += 1
                else:
                    flats += 1

        for event in events:
            close_until(event["opened"])
            profit_steps = max(0, int(math.floor(max(0.0, balance - start_balance) / 200.0)))
            if profit_steps != last_steps:
                lot_milestones.append({"time": event["opened"].isoformat(), "balance": round(balance, 2), "steps": profit_steps})
                last_steps = profit_steps
            if event["source"] == "scalper":
                lot = 0.05 + (profit_steps * 0.01)
            elif event["module"] == "phoenix_early_range":
                risk_usd = max(0.0, balance) * 0.01
                loss_per_lot = abs(_profit(symbol, event["side"], 1.0, event["entry"], event["entry"] - 6.0 if event["side"] == "buy" else event["entry"] + 6.0))
                lot = min(0.20, risk_usd / loss_per_lot if loss_per_lot > 0 else 0.01)
            else:
                base = _channel_override(cfg, int(event["chat_id"]))
                lot = float(base if base is not None else cfg.signal_fixed_lot) + (profit_steps * 0.01)
            if float(args.max_leg_lot) > 0:
                lot = min(lot, float(args.max_leg_lot))
            lot = normalize_volume(symbol, lot, float(cfg.min_lot), float(cfg.max_lot))
            margin = _margin(symbol, event["side"], lot, event["entry"])
            if margin > max(0.0, balance - used_margin) + 0.01:
                skipped_margin += 1
                continue
            gross = float(event["profit_001"]) * (lot / 0.01)
            spread_cost = float(event.get("spread_per_001", 0.0) or 0.0) * (lot / 0.01)
            initial_sl = float(event.get("initial_sl", 0.0) or 0.0)
            initial_risk = abs(_profit(symbol, event["side"], lot, event["entry"], initial_sl)) if initial_sl > 0 else 0.0
            trade = {**event, "lot": lot, "margin": margin, "initial_risk": initial_risk, "pnl": gross - spread_cost}
            sequence += 1
            heapq.heappush(open_heap, (event["closed"], sequence, trade))
            used_margin += margin
            open_risk += initial_risk
            accepted += 1
            max_margin = max(max_margin, used_margin)
            max_open_risk = max(max_open_risk, open_risk)
            max_concurrent = max(max_concurrent, len(open_heap))
            max_total_lot = max(max_total_lot, sum(float(row[2]["lot"]) for row in open_heap))

        close_until(datetime.max.replace(tzinfo=UTC))
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "sessions": len(session_dates),
            "session_dates": session_dates,
            "cutoff_utc": cutoff.isoformat(),
            "start_balance": start_balance,
            "final_balance": round(balance, 2),
            "profit": round(balance - start_balance, 2),
            "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
            "max_closed_drawdown_usd": round(max_dd, 2),
            "max_closed_drawdown_pct_start": round(abs(max_dd) / start_balance * 100.0, 2),
            "max_closed_drawdown_pct_peak": round(max_dd_pct_peak, 2),
            "minimum_closed_balance": round(min_balance, 2),
            "max_simultaneous_initial_sl_risk": round(max_open_risk, 2),
            "candidate_legs": len(events),
            "accepted_legs": accepted,
            "skipped_margin": skipped_margin,
            "wins": wins,
            "losses": losses,
            "flat": flats,
            "win_rate_decided_pct": round(100.0 * wins / max(1, wins + losses), 2),
            "active_days": len(daily),
            "average_active_day_pnl": round((balance - start_balance) / max(1, len(daily)), 2),
            "best_day": max(daily.items(), key=lambda item: item[1]) if daily else None,
            "worst_day": min(daily.items(), key=lambda item: item[1]) if daily else None,
            "max_concurrent_positions": max_concurrent,
            "max_concurrent_lot": round(max_total_lot, 2),
            "max_used_margin": round(max_margin, 2),
            "by_source": {key: round(value, 2) for key, value in sorted(by_source.items(), key=lambda item: item[1], reverse=True)},
            "by_module": {key: round(value, 2) for key, value in sorted(by_module.items(), key=lambda item: item[1], reverse=True)},
            "by_channel": {
                key: {"pnl": round(value, 2), "wins": by_channel_count[key][0], "losses": by_channel_count[key][1], "flat": by_channel_count[key][2]}
                for key, value in sorted(by_channel.items(), key=lambda item: item[1], reverse=True)
            },
            "daily_pnl": {key: round(value, 2) for key, value in sorted(daily.items())},
            "lot_rule": {
                "listener": "channel base leg lot +0.01 per closed +200 USD",
                "scalper": "0.05 per leg +0.01 per closed +200 USD",
                "phoenix_early_range": "1% balance risk with 6 USD SL, capped at 0.20 lot",
                "max_leg_lot": float(args.max_leg_lot),
            },
            "lot_milestones": lot_milestones,
            "limitations": [
                "Closed-balance drawdown only; intratrade floating drawdown is not reconstructed.",
                "Scalper setup stream comes from its standalone current-profile run; listener PnL can change a shared-account daily profit-lock threshold in live trading.",
                "Historical Telegram edits are evaluated at stored message content and may include information edited after original publication.",
            ],
        }
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(args.output)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
