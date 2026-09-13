from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.ghp_parser import parse_ghp_message
from app.mt5_gateway import Mt5Credentials, calc_loss_per_lot, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from app.telegram_signal_bot import _channel_strategy, _parse_signal, _relay_content_signature
from scripts.audit_ghp_parsers_90sessions import _attach_actions, _deduplicate, _rates, _resolve_symbol, _simulate as simulate_ghp
from scripts.backtest_phoenix_complete_60d import _rates as _chunked_rates
from scripts.simulate_current_eight_modules import _load, _phoenix_events


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _scalper_events(path: str, module: str) -> list[dict[str, Any]]:
    report = _load(path)
    source = report.get("trades", [])
    settings = report.get("settings", {})
    risk_pct_override = float(settings.get("risk_pct_per_leg", 0.0) or 0.0)
    if any("initial_sl" not in row for row in source):
        raise ValueError(f"{path}: rerun the scalper test to export original SL; exit prices cannot determine initial risk")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        grouped[str(row["opened"])].append(row)
    events: list[dict[str, Any]] = []
    for rows in grouped.values():
        stop_candidates: list[float] = []
        loss_candidates: list[float] = []
        for row in rows:
            entry = float(row.get("entry", 0.0) or 0.0)
            if "initial_sl" in row:
                stop_candidates.append(abs(entry - float(row["initial_sl"])))
                continue
            exit_price = float(row.get("exit", 0.0) or 0.0)
            distance = abs(exit_price - entry)
            match = re.search(r"_([0-9]+(?:\.[0-9]+)?)$", str(row.get("leg", "")))
            if str(row.get("status", "")) == "win" and match and float(match.group(1)) > 0.0:
                stop_candidates.append(distance / float(match.group(1)))
            if str(row.get("status", "")) == "loss" and distance > 0.0:
                loss_candidates.append(distance)
        if stop_candidates:
            stop_distance = sorted(stop_candidates)[len(stop_candidates) // 2]
        elif loss_candidates:
            stop_distance = max(loss_candidates)
        else:
            continue
        for row in rows:
            entry = float(row.get("entry", 0.0) or 0.0)
            side = str(row.get("side", "buy"))
            old_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
            events.append(
                {
                    "module": module,
                    "opened": _dt(row["opened"]),
                    "closed": _dt(row["closed"]),
                    "side": side,
                    "entry": entry,
                    "sl": float(row["initial_sl"]),
                    "pnl_per_lot": float(row.get("profit", 0.0) or 0.0) / old_lot,
                    "status": str(row.get("status", "")),
                    "risk_pct_override": risk_pct_override,
                }
            )
    return events


def _phoenix_extra_events(path: str, min_rr: float) -> list[dict[str, Any]]:
    events = []
    for row in _load(path).get("trades", []):
        entry = float(row.get("entry", 0.0) or 0.0)
        sl = float(row.get("sl", 0.0) or 0.0)
        tp = float(row.get("tp", 0.0) or 0.0)
        risk = abs(entry - sl)
        if risk <= 0.0 or abs(tp - entry) / risk < min_rr:
            continue
        events.extend(_phoenix_events_from_rows([row], "phoenix_extra_market"))
    return events


def _phoenix_events_from_rows(rows: list[dict[str, Any]], module: str) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        entry = float(row.get("entry", 0.0) or 0.0)
        sl = float(row.get("sl", 0.0) or 0.0)
        if entry <= 0.0 or sl <= 0.0 or abs(entry - sl) < 0.01:
            continue
        events.append(
            {
                "module": module,
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row.get("side", "buy")),
                "entry": entry,
                "sl": sl,
                "pnl_001": float(row.get("pnl_001", row.get("pnl", 0.0)) or 0.0),
                "status": str(row.get("status", "")),
                "message_id": int(row.get("message_id", 0) or 0),
                "target_index": int(row.get("target_index", 0) or 0),
                "fill_kind": str(row.get("fill_kind", "")),
            }
        )
    return events


def _active_phoenix_events(path: str, targets: set[int], leg_lot: float) -> list[dict[str, Any]]:
    rows = [row for row in _load(path).get("trades", []) if int(row.get("target_index", 0) or 0) in targets]
    events = _phoenix_events_from_rows(rows, "phoenix_active")
    return [{**row, "fixed_lot": float(leg_lot)} for row in events]


def _current_events(
    symbol: str,
    start: datetime,
    end: datetime,
    scalper_db60: str,
    scalper_bbkelt: str,
) -> list[dict[str, Any]]:
    inputs = {
        "phoenix_range": "data_vantage/phoenix_active_range_60sessions_20260903.json",
        "phoenix_direction": "data_vantage/phoenix_direction_current_60sessions_20260903.json",
        "phoenix_tp1_runner": "data_vantage/phoenix_tp1_runner_cap12_reward15_60sessions_20260903.json",
        "phoenix_profit": "data_vantage/phoenix_profit_variant_60sessions_20260903.json",
        "phoenix_extra_market": "data_vantage/phoenix_extra_market_tp2_tp5_60sessions_20260903.json",
        "scalper_db60": scalper_db60,
        "scalper_bbkelt": scalper_bbkelt,
    }
    for module in inputs:
        inputs[module] = os.getenv("AUDIT_SOURCE_" + module.upper(), inputs[module])
    enabled_flags = {
        "phoenix_range": "PHOENIX_RANGE_TRIGGER_ENABLED",
        "phoenix_direction": "PHOENIX_DIRECTION_RUNNER_ENABLED",
        "phoenix_tp1_runner": "PHOENIX_EXTRA_MARKET_TP1_ENABLED",
        "phoenix_profit": "PHOENIX_PROFIT_MODULE_ENABLED",
        "phoenix_extra_market": "PHOENIX_EXTRA_TP_RUNNER_ENABLED",
    }
    rows = [
        *_phoenix_events(inputs["phoenix_range"], "phoenix_range"),
        *_phoenix_events(inputs["phoenix_direction"], "phoenix_direction"),
        *_phoenix_events(inputs["phoenix_tp1_runner"], "phoenix_tp1_runner", "tp1_runner"),
        *_phoenix_events(inputs["phoenix_profit"], "phoenix_profit"),
        *_phoenix_extra_events(
            inputs["phoenix_extra_market"],
            max(0.0, float(os.getenv("PHOENIX_EXTRA_TP_RUNNER_MIN_RR", "0") or 0.0)),
        ),
        *_scalper_events(inputs["scalper_db60"], "scalper_db60"),
        *_scalper_events(inputs["scalper_bbkelt"], "scalper_bbkelt"),
    ]
    output = []
    for row in rows:
        flag = enabled_flags.get(row["module"])
        if flag and os.getenv(flag, "false").lower() not in {"true", "1", "yes", "on"}:
            continue
        if not (start <= row["opened"] <= end):
            continue
        if "loss_per_lot" not in row:
            loss = calc_loss_per_lot(symbol, row["side"], row["entry"], row["sl"])
            if loss <= 0:
                continue
            pnl_per_lot = row.get("pnl_per_lot")
            if pnl_per_lot is None:
                pnl_per_lot = row["pnl_001"] * 100.0
            row = {**row, "loss_per_lot": loss, "pnl_per_lot": float(pnl_per_lot)}
        output.append({**row, "symbol": symbol})
    return output


def _ghp_events(
    raw_path: Path,
    start: datetime,
    end: datetime,
    allowed_channels: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    with raw_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if allowed_channels and str(row.get("channel", "")) not in allowed_channels:
                continue
            row["date_dt"] = _dt(row["date"])
            if not (start <= row["date_dt"] <= end):
                continue
            row["parsed"] = vars(parse_ghp_message(row["text"], row["title"])).copy()
            signal = row["parsed"].get("signal")
            row["parsed"]["signal"] = vars(signal).copy() if signal else None
            row.pop("actions", None)
            rows.append(row)
    _attach_actions(rows)
    unique, _ = _deduplicate([row for row in rows if row["parsed"].get("signal")])
    assets = sorted({row["parsed"]["signal"]["asset"] for row in unique})
    symbols = {asset: _resolve_symbol(asset) for asset in assets}
    frames = {
        asset: _rates(symbol, start - timedelta(hours=4), end + timedelta(hours=73))
        for asset, symbol in symbols.items()
        if symbol
    }
    output = []
    for row in unique:
        asset = row["parsed"]["signal"]["asset"]
        symbol = symbols.get(asset)
        frame = frames.get(asset)
        if not symbol or frame is None or frame.empty:
            continue
        signal = row["parsed"]["signal"]
        start_idx = int(frame["time"].searchsorted(row["date_dt"], side="left"))
        if start_idx >= len(frame):
            continue
        market_price = float(frame.iloc[start_idx]["open"])
        if asset == "gold":
            entries = [float(value) for value in signal.get("entries", []) if float(value or 0.0) > 0.0]
            anchor = (max(entries) if signal.get("side") == "buy" else min(entries)) if entries else 0.0
            sl = float(signal.get("sl", 0.0) or 0.0)
            if not entries or sl <= 0.0 or min(abs(entry - sl) for entry in entries) > 40.0:
                continue
            # Explicit GHP prices belong to the provider. Never translate the
            # package to the current quote; wait at the original level instead.
        if row["channel"] in {"ghptrading", "-1001958009741"}:
            # Validated GHP Gold profile: three target branches share the
            # provider's midpoint entry instead of biasing legs to range edges.
            signal = row["parsed"]["signal"]
            levels = sorted({float(value) for value in signal.get("entries", []) if float(value or 0.0) > 0.0})
            midpoint = sum(levels) / max(1, len(levels))
            execution_plans = []
            for target_index, be in zip((1, 2, 99), (False, True, True)):
                pending_kind = (
                    ("stop" if market_price < midpoint else "limit")
                    if signal.get("side") == "buy"
                    else ("stop" if market_price > midpoint else "limit")
                )
                leg_row = {
                    **row,
                    "parsed": {
                        **row["parsed"],
                        "signal": {**signal, "entries": [midpoint], "order_kind": pending_kind},
                    },
                }
                execution_plans.append((target_index, be, leg_row))
        elif row["channel"] == "-1003495213392":
            if asset not in {"audusd", "chfjpy", "euraud", "eurjpy", "eurusd", "gbpjpy", "usdchf"}:
                continue
            currency_row = {
                **row,
                "actions": [
                    action
                    for action in row.get("actions", [])
                    if action.get("kind") != "breakeven"
                ],
            }
            execution_plans = [(1, False, currency_row)]
        elif row["channel"] == "-1003306025363" and asset != "ger40":
            continue
        else:
            execution_plans = [(1, False, row)]
        plan_count = len(execution_plans)
        split_lots = [0.15] if plan_count == 1 else ([0.08, 0.07] if plan_count == 2 else [0.05] * plan_count)
        for plan_offset, (target_index, be, simulation_row) in enumerate(execution_plans):
            expiry_minutes = (
                max(1, int(float(os.getenv("GHP_GOLD_PENDING_EXPIRY_MINUTES", "60") or 60)))
                if row["channel"] in {"ghptrading", "-1001958009741"}
                else 60
            )
            outcome = simulate_ghp(simulation_row, frame, symbol, target_index, expiry_minutes, be)
            if outcome["status"] in {"expired", "cancelled", "invalid", "no_rates"} or "opened" not in outcome:
                continue
            loss = calc_loss_per_lot(symbol, row["parsed"]["signal"]["side"], outcome["entry"], outcome["sl"])
            if loss <= 0:
                continue
            output.append(
                {
                    "module": f"ghp:{row['channel']}",
                    "symbol": symbol,
                    "opened": outcome["opened"],
                    "closed": outcome["closed"],
                    "side": row["parsed"]["signal"]["side"],
                    "asset": asset,
                    "target_index": int(target_index),
                    "entry": outcome["entry"],
                    "sl": float(outcome["sl"]),
                    "loss_per_lot": loss,
                    "pnl_per_lot": outcome["pnl_001"] * 100.0,
                    "status": outcome["status"],
                    "fixed_lot": split_lots[plan_offset],
                    "message_id": int(row.get("message_id", 0) or 0),
                }
            )
    return output


def _dany_events(raw_path: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    payload = _load(raw_path)
    source = payload.get("messages", []) if isinstance(payload, dict) else []
    parsed_rows = []
    recent_signatures: dict[str, datetime] = {}
    for row in sorted(source, key=lambda item: str(item.get("date", ""))):
        moment = _dt(row["date"])
        if not (start <= moment <= end):
            continue
        signal = _parse_signal(
            str(row.get("text", "")),
            f"-1004410781005:{int(row.get('id', 0) or 0)}",
            -1004410781005,
            "Dany Signals",
            "",
            int(row.get("id", 0) or 0),
        )
        if signal is None:
            continue
        signature = _relay_content_signature(signal)
        previous = recent_signatures.get(signature)
        if previous is not None and moment - previous <= timedelta(minutes=30):
            continue
        recent_signatures[signature] = moment
        parsed_rows.append((row, moment, signal, _channel_strategy(signal)))

    assets = sorted({signal.asset for _, _, signal, _ in parsed_rows})
    symbols = {asset: _resolve_symbol(asset) for asset in assets}
    frames = {
        asset: _rates(symbol, start - timedelta(hours=4), end + timedelta(hours=73))
        for asset, symbol in symbols.items()
        if symbol
    }
    output = []
    for row, moment, signal, strategy in parsed_rows:
        symbol = symbols.get(signal.asset)
        frame = frames.get(signal.asset)
        if not symbol or frame is None or frame.empty or not signal.entries or signal.sl <= 0 or not signal.tps:
            continue
        plans = tuple(strategy.split_target_indices) or (int(strategy.target_index),)
        modes = tuple(strategy.split_protect_modes) or tuple(strategy.protect_mode for _ in plans)
        midpoint = sum(signal.entries) / len(signal.entries)
        start_idx = int(frame["time"].searchsorted(moment, side="left"))
        if start_idx >= len(frame):
            continue
        market_price = float(frame.iloc[start_idx]["open"])
        order_kind = signal.order_kind
        if order_kind == "market":
            favorable = (
                signal.side == "buy" and midpoint <= market_price < signal.tps[0]
            ) or (
                signal.side == "sell" and signal.tps[0] < market_price <= midpoint
            )
            if not favorable:
                order_kind = (
                    ("stop" if market_price < midpoint else "limit")
                    if signal.side == "buy"
                    else ("stop" if market_price > midpoint else "limit")
                )
        simulation_row = {
            "date_dt": moment,
            "actions": [],
            "parsed": {
                "signal": {
                    "side": signal.side,
                    "asset": signal.asset,
                    "entries": [midpoint],
                    "sl": signal.sl,
                    "tps": signal.tps,
                    "order_kind": order_kind,
                }
            },
        }
        for offset, target_index in enumerate(plans):
            mode = modes[min(offset, len(modes) - 1)] if modes else "none"
            outcome = simulate_ghp(
                simulation_row,
                frame,
                symbol,
                int(target_index),
                max(1, int(strategy.pending_expiry_minutes or 60)),
                mode in {"be", "be_after_tp1", "be_after_tp3"},
            )
            if outcome["status"] in {"expired", "cancelled", "invalid", "no_rates"} or "opened" not in outcome:
                continue
            loss = calc_loss_per_lot(symbol, signal.side, float(outcome["entry"]), float(outcome["sl"]))
            if loss <= 0:
                continue
            output.append(
                {
                    "module": "dany_signals",
                    "symbol": symbol,
                    "opened": outcome["opened"],
                    "closed": outcome["closed"],
                    "side": signal.side,
                    "asset": signal.asset,
                    "target_index": int(target_index),
                    "entry": float(outcome["entry"]),
                    "sl": float(outcome["sl"]),
                    "loss_per_lot": loss,
                    "pnl_per_lot": float(outcome["pnl_001"]) * 100.0,
                    "status": outcome["status"],
                    "message_id": int(row.get("id", 0) or 0),
                }
            )
    return output


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0)


def _portfolio(
    events: list[dict[str, Any]],
    cfg: Any,
    start_balance: float,
    risk_pct: float,
    enforce_margin: bool,
    daily_profit_lock_pct: float = 0.0,
    daily_loss_lock_pct: float = 0.0,
) -> dict[str, Any]:
    balance = start_balance
    peak = balance
    minimum = balance
    max_dd = 0.0
    used_margin = 0.0
    max_margin = 0.0
    max_dd_pct = 0.0
    max_risk = 0.0
    allocated_risk = 0.0
    max_concurrent = 0
    skipped = 0
    skipped_for_daily_lock = 0
    seq = 0
    heap = []
    stats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    daily: dict[str, float] = defaultdict(float)
    ledger: list[dict[str, Any]] = []

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, minimum, max_dd, used_margin, allocated_risk, max_dd_pct
        while heap and heap[0][0] <= moment:
            _, _, trade = heapq.heappop(heap)
            used_margin = max(0.0, used_margin - trade["margin"])
            allocated_risk = max(0.0, allocated_risk - trade["risk"])
            balance_before = balance
            balance += trade["pnl"]
            peak = max(peak, balance)
            minimum = min(minimum, balance)
            max_dd = max(max_dd, peak - balance)
            max_dd_pct = max(max_dd_pct, 100.0 * (peak - balance) / max(0.01, peak))
            bucket = stats[trade["module"]]
            bucket["positions"] += 1
            bucket["pnl"] += trade["pnl"]
            bucket["wins" if trade["pnl"] > 0.005 else "losses" if trade["pnl"] < -0.005 else "flat"] += 1
            daily[trade["closed"].date().isoformat()] += trade["pnl"]
            ledger.append(
                {
                    "opened": trade["opened"].isoformat(),
                    "closed": trade["closed"].isoformat(),
                    "module": trade["module"],
                    "message_id": int(trade.get("message_id", 0) or 0),
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "target_index": int(trade.get("target_index", 0) or 0),
                    "fill_kind": str(trade.get("fill_kind", "")),
                    "status": trade["status"],
                    "entry": round(float(trade.get("entry", 0.0) or 0.0), 8),
                    "sl": round(float(trade.get("sl", 0.0) or 0.0), 8),
                    "lot": round(float(trade["lot"]), 4),
                    "risk_pct": round(float(trade.get("risk_pct_override", risk_pct)), 4),
                    "initial_risk": round(float(trade.get("risk", 0.0) or 0.0), 2),
                    "margin": round(float(trade.get("margin", 0.0) or 0.0), 2),
                    "pnl_per_lot": round(float(trade.get("pnl_per_lot", 0.0) or 0.0), 8),
                    "pnl": round(float(trade["pnl"]), 2),
                    "balance_before": round(float(balance_before), 2),
                    "balance_after": round(float(balance), 2),
                }
            )

    for event in sorted(events, key=lambda item: (item["opened"], item["closed"])):
        close_until(event["opened"])
        day_key = event["opened"].date().isoformat()
        day_profit = float(daily.get(day_key, 0.0) or 0.0)
        day_start_balance = max(0.01, balance - day_profit)
        profit_locked = daily_profit_lock_pct > 0.0 and day_profit >= day_start_balance * daily_profit_lock_pct / 100.0
        loss_locked = daily_loss_lock_pct > 0.0 and day_profit <= -day_start_balance * daily_loss_lock_pct / 100.0
        if profit_locked or loss_locked:
            skipped_for_daily_lock += 1
            continue
        info = mt5.symbol_info(event["symbol"])
        broker_min = float(getattr(info, "volume_min", cfg.min_lot) or cfg.min_lot)
        event_risk_pct = float(event.get("risk_pct_override", 0.0) or risk_pct)
        requested_risk = max(0.0, balance) * event_risk_pct / 100.0
        raw_lot = float(event["fixed_lot"]) if "fixed_lot" in event else requested_risk / max(0.01, event["loss_per_lot"])
        lot = normalize_volume(event["symbol"], max(broker_min, raw_lot), broker_min, max(float(cfg.max_lot), 100.0))
        actual_risk = lot * event["loss_per_lot"]
        margin = _margin(event["symbol"], event["side"], lot, event["entry"])
        if enforce_margin and (balance <= 0 or margin > balance - used_margin + 0.01):
            skipped += 1
            continue
        trade = {**event, "lot": lot, "risk": actual_risk, "margin": margin, "pnl": event["pnl_per_lot"] * lot}
        seq += 1
        heapq.heappush(heap, (trade["closed"], seq, trade))
        used_margin += margin
        allocated_risk += actual_risk
        max_margin = max(max_margin, used_margin)
        max_risk = max(max_risk, allocated_risk)
        max_concurrent = max(max_concurrent, len(heap))
    close_until(datetime.max.replace(tzinfo=UTC))

    by_module = {}
    for module, values in sorted(stats.items()):
        decided = values["wins"] + values["losses"]
        by_module[module] = {
            "positions": int(values["positions"]),
            "win_rate_pct": round(100.0 * values["wins"] / max(1.0, decided), 2),
            "pnl": round(values["pnl"], 2),
        }
    return {
        "status": "STOP_OUT" if minimum <= 0.0 else "COMPLETED",
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "minimum_closed_balance": round(minimum, 2),
        "max_closed_drawdown_usd": round(max_dd, 2),
        "max_closed_drawdown_pct_peak": round(max_dd_pct, 2),
        "max_concurrent_positions": max_concurrent,
        "max_allocated_initial_risk_usd": round(max_risk, 2),
        "max_used_margin": round(max_margin, 2),
        "skipped_for_margin": skipped,
        "skipped_for_daily_lock": skipped_for_daily_lock,
        "by_module": by_module,
        "daily_pnl": {key: round(value, 2) for key, value in sorted(daily.items())},
        "trade_sequence": ledger,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-env", default="")
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--messages", default="data_vantage/ghp_parser_audit_90sessions_20260904_v2_messages.jsonl")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct-per-leg", type=float, default=1.5)
    parser.add_argument("--daily-profit-lock-pct", type=float, default=0.0)
    parser.add_argument("--daily-loss-lock-pct", type=float, default=0.0)
    parser.add_argument("--daily-profit-lock-grid", default="")
    parser.add_argument("--daily-loss-lock-grid", default="")
    parser.add_argument("--signal-env")
    parser.add_argument("--phoenix-active")
    parser.add_argument("--phoenix-profit-extra", default="")
    parser.add_argument("--phoenix-range-active", default="")
    parser.add_argument("--phoenix-range-market-risk-multiplier", type=float, default=1.0)
    parser.add_argument("--phoenix-active-targets", default="1,2,4")
    parser.add_argument("--phoenix-extra-target-risk-multiplier", type=float, default=1.0)
    parser.add_argument("--phoenix-leg-lot", type=float, default=0.05)
    parser.add_argument("--signal-risk-pct-per-leg", type=float, default=0.0)
    parser.add_argument("--override-all-risk-pct", type=float, default=0.0)
    parser.add_argument("--phoenix-range-risk-pct", type=float, default=0.0)
    parser.add_argument("--no-ghp", action="store_true")
    parser.add_argument("--no-scalpers", action="store_true")
    parser.add_argument("--ghp-channels", default="")
    parser.add_argument("--dany-messages", default="")
    parser.add_argument(
        "--scalper-db60",
        default="data_vantage/scalper_db60_optimized_risk15_60sessions_20260903.json",
    )
    parser.add_argument(
        "--scalper-bbkelt",
        default="data_vantage/scalper_bbkelt_profit_risk15_corrected_60sessions_20260903.json",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.base_env:
        load_dotenv(args.base_env, override=True)
    load_dotenv(args.env, override=True)
    if args.signal_env:
        load_dotenv(args.signal_env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        xau = ensure_symbol(cfg.symbol)
        probe = mt5.copy_rates_from_pos(xau, mt5.TIMEFRAME_D1, 0, args.sessions + 20)
        dates = sorted({datetime.fromtimestamp(int(row["time"]), UTC).date() for row in probe})[-args.sessions:]
        start = datetime.combine(dates[0], datetime.min.time(), tzinfo=UTC)
        end = datetime.now(UTC)
        if args.phoenix_active:
            active_targets = {
                int(value.strip())
                for value in str(args.phoenix_active_targets).split(",")
                if value.strip()
            }
            events = _active_phoenix_events(args.phoenix_active, active_targets, args.phoenix_leg_lot)
            if args.phoenix_profit_extra:
                profit_rows = _load(args.phoenix_profit_extra).get("trades", [])
                events += _phoenix_events_from_rows(profit_rows, "phoenix_profit")
            if args.phoenix_range_active:
                range_rows = _load(args.phoenix_range_active).get("trades", [])
                events += _phoenix_events_from_rows(range_rows, "phoenix_range")
            if not args.no_scalpers:
                events += _scalper_events(args.scalper_db60, "scalper_db60")
                events += _scalper_events(args.scalper_bbkelt, "scalper_bbkelt")
            events = [row if "symbol" in row else {**row, "symbol": xau} for row in events if start <= row["opened"] <= end]
            normalized_events = []
            for row in events:
                if "loss_per_lot" not in row:
                    loss = calc_loss_per_lot(xau, row["side"], row["entry"], row["sl"])
                    if loss <= 0:
                        continue
                    pnl_per_lot = row.get("pnl_per_lot")
                    if pnl_per_lot is None:
                        pnl_per_lot = float(row["pnl_001"]) * 100.0
                    row = {**row, "loss_per_lot": loss, "pnl_per_lot": float(pnl_per_lot)}
                normalized_events.append(row)
            events = normalized_events
        else:
            events = _current_events(
                xau,
                start,
                end,
                args.scalper_db60,
                args.scalper_bbkelt,
            )
        if not args.no_ghp:
            selected_ghp_channels = {
                value.strip() for value in str(args.ghp_channels).split(",") if value.strip()
            }
            events += _ghp_events(
                ROOT / args.messages,
                start,
                end,
                selected_ghp_channels or None,
            )
        if args.dany_messages:
            events += _dany_events(ROOT / args.dany_messages, start, end)
        if args.signal_risk_pct_per_leg > 0.0:
            events = [
                (
                    {**{key: value for key, value in row.items() if key != "fixed_lot"}, "risk_pct_override": args.signal_risk_pct_per_leg}
                    if row["module"].startswith("phoenix_") or row["module"].startswith("ghp:")
                    else row
                )
                for row in events
            ]
        if args.phoenix_extra_target_risk_multiplier != 1.0:
            core_targets = {1, 2, 4}
            events = [
                (
                    {
                        **row,
                        "risk_pct_override": float(row.get("risk_pct_override", args.risk_pct_per_leg))
                        * args.phoenix_extra_target_risk_multiplier,
                    }
                    if row["module"] == "phoenix_active"
                    and int(row.get("target_index", 0) or 0) not in core_targets
                    else row
                )
                for row in events
            ]
        if args.override_all_risk_pct > 0.0:
            events = [
                {
                    **{key: value for key, value in row.items() if key != "fixed_lot"},
                    "risk_pct_override": args.override_all_risk_pct,
                }
                for row in events
            ]
        if args.phoenix_range_risk_pct > 0.0:
            events = [
                (
                    {**row, "risk_pct_override": args.phoenix_range_risk_pct}
                    if row["module"] == "phoenix_range"
                    else row
                )
                for row in events
            ]
        if args.phoenix_range_market_risk_multiplier != 1.0:
            events = [
                (
                    {
                        **row,
                        "risk_pct_override": float(row.get("risk_pct_override", args.risk_pct_per_leg))
                        * max(0.0, args.phoenix_range_market_risk_multiplier),
                    }
                    if row["module"] == "phoenix_range" and row.get("fill_kind") == "market"
                    else row
                )
                for row in events
            ]
        realistic = _portfolio(
            events,
            cfg,
            args.start_balance,
            args.risk_pct_per_leg,
            True,
            args.daily_profit_lock_pct,
            args.daily_loss_lock_pct,
        )
        theoretical = _portfolio(
            events,
            cfg,
            args.start_balance,
            args.risk_pct_per_leg,
            False,
            args.daily_profit_lock_pct,
            args.daily_loss_lock_pct,
        )
        lock_grid = []
        profit_grid = [
            float(value.strip())
            for value in args.daily_profit_lock_grid.split(",")
            if value.strip()
        ]
        loss_grid = [
            float(value.strip())
            for value in args.daily_loss_lock_grid.split(",")
            if value.strip()
        ]
        if profit_grid or loss_grid:
            profit_grid = profit_grid or [0.0]
            loss_grid = loss_grid or [0.0]
            holdout_start = datetime.combine(dates[len(dates) // 2], datetime.min.time(), tzinfo=UTC)
            holdout_events = [row for row in events if row["opened"] >= holdout_start]
            for profit_lock, loss_lock in product(profit_grid, loss_grid):
                full_result = _portfolio(
                    events,
                    cfg,
                    args.start_balance,
                    args.risk_pct_per_leg,
                    True,
                    profit_lock,
                    loss_lock,
                )
                holdout_result = _portfolio(
                    holdout_events,
                    cfg,
                    args.start_balance,
                    args.risk_pct_per_leg,
                    True,
                    profit_lock,
                    loss_lock,
                )
                lock_grid.append(
                    {
                        "daily_profit_lock_pct": profit_lock,
                        "daily_loss_lock_pct": loss_lock,
                        "full": {key: full_result[key] for key in (
                            "final_balance",
                            "profit",
                            "return_pct",
                            "max_closed_drawdown_pct_peak",
                            "skipped_for_daily_lock",
                        )},
                        "holdout": {key: holdout_result[key] for key in (
                            "final_balance",
                            "profit",
                            "return_pct",
                            "max_closed_drawdown_pct_peak",
                            "skipped_for_daily_lock",
                        )},
                    }
                )
    finally:
        shutdown()
    configured_ghp_modules = (
        [f"ghp:{value.strip()}" for value in str(args.ghp_channels).split(",") if value.strip()]
        or ["ghp:ghptrading", "ghp:-1001958009741", "ghp:-1003306025363", "ghp:-1003495213392"]
    )
    if args.phoenix_active:
        expected_modules = ["phoenix_active"]
        if args.phoenix_profit_extra:
            expected_modules.append("phoenix_profit")
        if args.phoenix_range_active:
            expected_modules.append("phoenix_range")
        if not args.no_scalpers:
            expected_modules.extend(["scalper_db60", "scalper_bbkelt"])
    else:
        expected_modules = ["scalper_db60", "scalper_bbkelt"]
        for module, flag in {
            "phoenix_range": "PHOENIX_RANGE_TRIGGER_ENABLED",
            "phoenix_direction": "PHOENIX_DIRECTION_RUNNER_ENABLED",
            "phoenix_tp1_runner": "PHOENIX_EXTRA_MARKET_TP1_ENABLED",
            "phoenix_profit": "PHOENIX_PROFIT_MODULE_ENABLED",
            "phoenix_extra_market": "PHOENIX_EXTRA_TP_RUNNER_ENABLED",
        }.items():
            if os.getenv(flag, "false").lower() in {"true", "1", "yes", "on"}:
                expected_modules.append(module)
    if not args.no_ghp:
        expected_modules.extend(configured_ghp_modules)
    if args.dany_messages:
        expected_modules.append("dany_signals")
    actual_modules = sorted({row["module"] for row in events})
    missing_modules = sorted(set(expected_modules) - set(actual_modules))
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range": {"start": start.isoformat(), "end": end.isoformat(), "sessions": len(dates)},
        "event_count": len(events),
        "module_count": len({row["module"] for row in events}),
        "event_count_by_module": dict(sorted(Counter(row["module"] for row in events).items())),
        "assumptions": {
            "start_balance": args.start_balance,
            "risk_pct_per_leg": args.risk_pct_per_leg,
            "daily_profit_lock_pct": args.daily_profit_lock_pct,
            "daily_loss_lock_pct": args.daily_loss_lock_pct,
            "phoenix_range_risk_pct_per_leg": args.phoenix_range_risk_pct,
            "phoenix_range_market_risk_multiplier": args.phoenix_range_market_risk_multiplier,
            "position_limit": "none",
            "execution_order": "chronological_by_open_and_close_time",
        },
        "realistic_with_broker_min_lot_and_margin": realistic,
        "without_margin_filter": theoretical,
        "daily_lock_grid": lock_grid,
        "data_quality": {
            "account_profile": str(args.env),
            "base_profile": str(args.base_env or ""),
            "signal_profile": str(args.signal_env or ""),
            "expected_modules": expected_modules,
            "actual_modules": actual_modules,
            "missing_modules": missing_modules,
            "forecast_eligible": bool(
                not missing_modules
                and realistic["status"] == "COMPLETED"
                and realistic["skipped_for_margin"] == 0
            ),
        },
        "limitations": [
            "Closed-balance replay, not intrabar equity or broker stop-out simulation.",
            "Phoenix sources must be regenerated and checked against the current profile; filenames alone do not establish parity.",
            "GHP replay uses provider prices, final message text and simplified actions; not exact live market execution.",
            "Telegram does not supply historical versions of edited messages.",
        ],
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
