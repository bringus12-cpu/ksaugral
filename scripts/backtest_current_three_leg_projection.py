from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume
from app.telegram_signal_bot import (
    NEAR_ENTRY_MARKET_TOLERANCE,
    ParsedSignal,
    _channel_strategy,
    _channel_lot_override,
    _is_phoenix_source,
    _market_before_live_tp1,
    _parse_signal,
    _phoenix_entry_brain,
    _phoenix_market_runner_allowed,
    _phoenix_progressive_stop,
    _select_live_tps,
    _split_target_plan_for_strategy,
    _strategy_market_entry_allowed,
    _strict_live_tps_for_entry,
    _tp_one_runner_target_index,
)


@dataclass
class LegResult:
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    channel: str
    chat_id: int
    title: str
    message_id: int
    side: str
    entry: float
    initial_sl: float
    exit: float
    target_index: int
    plan_index: int
    order_kind: str
    protect_mode: str
    status: str
    profit_001: float
    lot_override: float | None


def _variants(token: str) -> set[str]:
    raw = str(token).strip()
    clean = raw.lower().lstrip("@")
    out = {clean}
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if clean.startswith(prefix):
            out.add(clean[len(prefix) :].strip("/"))
    if raw.startswith("-100"):
        out.add(raw[4:])
    return {item.strip("/") for item in out if item}


def _dialog_variants(dialog: Any) -> set[str]:
    did = str(getattr(dialog, "id", "") or "")
    username = str(getattr(dialog.entity, "username", "") or "").lower()
    out = {did, did.lstrip("-"), username}
    if did.startswith("-100"):
        out.add(did[4:])
    return {item for item in out if item}


def _timeframe(name: str) -> int:
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
    }[name.upper()]


def _rates(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, _timeframe(timeframe), start, end)
    if raw is None or len(raw) == 0:
        # Some MT5 terminals reject long M1 range requests even when the
        # requested candles are available in the local history cache.
        raw = mt5.copy_rates_from_pos(symbol, _timeframe(timeframe), 0, 99_999)
        if raw is None or len(raw) == 0:
            return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    frame = frame[(frame["time"] >= start_ts) & (frame["time"] <= end_ts)]
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _hit_tp(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _hit_sl(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _entry_touched(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _price_reached(side: str, price: float, level: float) -> bool:
    return price >= level if side == "buy" else price <= level


def _better_stop(side: str, current_sl: float, candidate: float) -> float:
    if current_sl <= 0:
        return candidate
    return max(current_sl, candidate) if side == "buy" else min(current_sl, candidate)


def _pending_valid(side: str, order_kind: str, entry: float, market_price: float) -> bool:
    if order_kind == "limit":
        return entry < market_price if side == "buy" else entry > market_price
    if order_kind == "stop":
        return entry > market_price if side == "buy" else entry < market_price
    return True


def _auto_sl_from_history(signal: ParsedSignal, symbol: str, entry: float, row: pd.Series, atr_mult: float, min_points: int) -> float:
    info = mt5.symbol_info(symbol)
    point = float(getattr(info, "point", 0.01) or 0.01)
    digits = int(getattr(info, "digits", 2) or 2)
    atr = float(row.get("atr14", 0.0) or 0.0)
    offset = max(point * float(min_points), atr * max(1.0, float(atr_mult)))
    return round(entry - offset if signal.side == "buy" else entry + offset, digits)


def _signal_plan(
    signal: ParsedSignal,
    market_price: float,
    *,
    add_test_market_tp1: bool = False,
    runner_spread_price: float = 0.0,
) -> list[tuple[float, bool, str, int, str, int]]:
    strategy = _channel_strategy(signal)
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    default_entry = float(signal.entry or 0.0) or market_price
    is_phoenix = _is_phoenix_source(signal.chat_id, signal.chat_title)
    if is_phoenix and str(os.getenv("PHOENIX_COMPLETE_CAPTURE_PROFILE", "false")).strip().lower() in {"1", "true", "yes", "on"}:
        brain = _phoenix_entry_brain(signal, market_price, entries)
        if brain.get("decision") in {"skip_too_late_after_tp", "skip_not_enough_live_tps"}:
            return []
        capture_target = _tp_one_runner_target_index(
            signal.side,
            market_price,
            signal.tps,
            runner_spread_price,
        )
        return [(market_price, False, "market", capture_target, "none", capture_target)] if capture_target > 0 else []
    is_tfxc = "tfxc" in str(signal.chat_title or "").lower()
    target_plan = _split_target_plan_for_strategy(
        strategy,
        is_phoenix_signal=is_phoenix,
        is_tfxc_signal=is_tfxc,
    )
    use_all_entries = strategy.force_all_entries and len(entries) > 1 and strategy.entry_policy != "nearest_pending_15m"
    plan: list[tuple[float, bool, str, int, str, int]] = []
    if use_all_entries:
        planned = entries[: len(target_plan)]
        if len(planned) < len(target_plan):
            planned.extend([planned[-1]] * (len(target_plan) - len(planned)))
        market_runner_allowed = (
            _phoenix_market_runner_allowed(signal.side, entries, market_price, signal.tps)
            if is_phoenix
            else _strategy_market_entry_allowed(strategy, signal.side, entries, market_price, signal.tps)
        )
        runner_target_index = max(1, int(float(os.getenv("PHOENIX_MARKET_RUNNER_TARGET_INDEX", "1")))) if is_phoenix else 1
        runner_protect_mode = str(os.getenv("PHOENIX_MARKET_RUNNER_PROTECT_MODE", "be") or "be").strip().lower() if is_phoenix else "be"
        if market_runner_allowed:
            plan.append((market_price, False, "market", runner_target_index, runner_protect_mode, 0))
        for entry, (target_index, protect_mode, plan_index) in zip(planned, target_plan):
            plan.append((entry, True, "limit", target_index, protect_mode, plan_index))
    else:
        if strategy.entry_policy == "nearest_pending_15m" and entries:
            default_entry = min(entries, key=lambda value: abs(float(value) - market_price))
        if signal.order_kind in {"limit", "stop"} and default_entry > 0:
            entry, pending, order_kind = default_entry, True, signal.order_kind
        else:
            entry, pending, order_kind = market_price, False, "market"
        for target_index, protect_mode, plan_index in target_plan:
            plan.append((entry, pending, order_kind, target_index, protect_mode, plan_index))

    if add_test_market_tp1 and _market_before_live_tp1(signal.side, market_price, signal.tps):
        runner_target = _tp_one_runner_target_index(
            signal.side,
            market_price,
            signal.tps,
            runner_spread_price,
        )
        if runner_target > 0:
            plan.append((market_price, False, "market", runner_target, "be", 999))
    return plan


def _simulate_leg(
    signal: ParsedSignal,
    symbol: str,
    entry: float,
    is_pending: bool,
    order_kind: str,
    target_index: int,
    protect_mode: str,
    start_idx: int,
    rates: pd.DataFrame,
    cfg: Any,
    horizon_hours: int,
) -> dict[str, Any]:
    start_row = rates.iloc[start_idx]
    market_price = float(start_row["close"])
    if is_pending and not _pending_valid(signal.side, order_kind, entry, market_price):
        return {"status": "skipped", "reason": "pending_wrong_side"}

    try:
        tp1, target, live_index, _ = _select_live_tps(signal.side, entry, signal.tps, target_index)
    except ValueError as exc:
        return {"status": "skipped", "reason": str(exc)}

    sl = float(signal.sl or 0.0)
    if sl <= 0:
        sl = _auto_sl_from_history(signal, symbol, entry, start_row, cfg.signal_sl_atr_mult, cfg.signal_sl_min_points)
    strategy = _channel_strategy(signal)
    is_phoenix = _is_phoenix_source(signal.chat_id, signal.chat_title)
    if is_phoenix and str(os.getenv("PHOENIX_COMPLETE_CAPTURE_PROFILE", "false")).strip().lower() in {"1", "true", "yes", "on"}:
        stop_cap = max(0.0, float(os.getenv("PHOENIX_MAX_STOP_DISTANCE_USD", "8.0") or 8.0))
        if stop_cap > 0.0 and abs(float(entry) - float(sl)) > stop_cap:
            sl = float(entry) - stop_cap if signal.side == "buy" else float(entry) + stop_cap
    elif not is_phoenix:
        stop_cap = float(strategy.max_stop_distance or 0.0)
        if stop_cap <= 0.0 and ("XAU" in symbol.upper() or "GOLD" in symbol.upper()):
            stop_cap = 6.0
        if stop_cap > 0.0 and abs(float(entry) - float(sl)) > stop_cap:
            sl = float(entry) - stop_cap if signal.side == "buy" else float(entry) + stop_cap
    if signal.side == "buy" and (sl >= entry or target <= entry):
        return {"status": "skipped", "reason": "invalid_buy_levels"}
    if signal.side == "sell" and (sl <= entry or target >= entry):
        return {"status": "skipped", "reason": "invalid_sell_levels"}

    times = rates["time"]
    expiry_minutes = (
        float(strategy.pending_expiry_minutes or cfg.signal_pending_expiry_minutes)
        if is_pending
        else float(horizon_hours) * 60.0
    )
    if not is_pending:
        trigger_idx = start_idx
    else:
        trigger_idx = -1
        expiry_time = times.iloc[start_idx] + pd.Timedelta(minutes=expiry_minutes)
        expiry_idx = min(int(times.searchsorted(expiry_time, side="right")), len(rates))
        for idx in range(start_idx, expiry_idx):
            row = rates.iloc[idx]
            high = float(row["high"])
            low = float(row["low"])
            if _price_reached(signal.side, high if signal.side == "buy" else low, tp1):
                return {"status": "expired", "reason": "tp1_before_entry"}
            if len(signal.tps) > 1 and _price_reached(signal.side, high if signal.side == "buy" else low, float(signal.tps[1])):
                return {"status": "expired", "reason": "tp2_before_entry"}
            if _entry_touched(entry, high, low):
                trigger_idx = idx
                break
        if trigger_idx < 0:
            return {"status": "expired", "reason": "not_triggered"}

    end_time = times.iloc[trigger_idx] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(times.searchsorted(end_time, side="right")), len(rates))
    current_sl = sl
    tp1_seen = False
    tp3_seen = False
    reached_level = 0
    for idx in range(trigger_idx, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        if _hit_sl(signal.side, current_sl, high, low):
            return {"status": "loss", "entry": entry, "initial_sl": sl, "exit": current_sl, "entry_idx": trigger_idx, "exit_idx": idx, "target_index": live_index}
        if _hit_tp(signal.side, target, high, low):
            return {"status": "win", "entry": entry, "initial_sl": sl, "exit": target, "entry_idx": trigger_idx, "exit_idx": idx, "target_index": live_index}
        if _hit_tp(signal.side, tp1, high, low):
            tp1_seen = True
        if len(signal.tps) >= 3 and _hit_tp(signal.side, float(signal.tps[2]), high, low):
            tp3_seen = True
        for level, tp_value in enumerate(signal.tps, start=1):
            if _hit_tp(signal.side, float(tp_value), high, low):
                reached_level = max(reached_level, level)
        if tp1_seen:
            if protect_mode == "tp1":
                current_sl = _better_stop(signal.side, current_sl, tp1)
            elif protect_mode == "be":
                current_sl = _better_stop(signal.side, current_sl, entry)
            elif protect_mode == "tp1_after_tp3" and tp3_seen:
                current_sl = _better_stop(signal.side, current_sl, tp1)
            elif protect_mode == "be_after_tp3" and tp3_seen:
                current_sl = _better_stop(signal.side, current_sl, entry)
            elif protect_mode == "phoenix_ladder" and reached_level > 0:
                candidate = _phoenix_progressive_stop(signal.side, entry, signal.tps, reached_level, current_sl)
                if candidate is not None:
                    current_sl = candidate

    exit_idx = max(trigger_idx, end_idx - 1)
    return {
        "status": "timeout",
        "entry": entry,
        "initial_sl": sl,
        "exit": float(rates.iloc[exit_idx]["close"]),
        "entry_idx": trigger_idx,
        "exit_idx": exit_idx,
        "target_index": live_index,
    }


def _lot_for_net(net_profit: float, base_lot: float, step_usd: float, add_lot: float, max_lot: float) -> float:
    steps = max(0, math.floor(max(0.0, net_profit) / step_usd))
    return min(max_lot, base_lot + (steps * add_lot))


def _lot_for_balance(balance: float, base_lot: float, step_usd: float, add_lot: float, max_lot: float) -> float:
    steps = max(0, math.floor(max(0.0, balance) / step_usd))
    return min(max_lot, max(base_lot, steps * add_lot))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "true" if default else "false") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _phoenix_lot_for_balance(balance: float) -> tuple[float, int]:
    base_balance = max(0.0, float(os.getenv("PHOENIX_LOT_BASE_BALANCE_USD", "1000") or 1000.0))
    base_lot = max(0.01, float(os.getenv("PHOENIX_LOT_BASE_PER_POSITION", "0.10") or 0.10))
    step_usd = max(1.0, float(os.getenv("PHOENIX_LOT_BALANCE_STEP_USD", "500") or 500.0))
    step_lot = max(0.0, float(os.getenv("PHOENIX_LOT_STEP_ADD", "0.01") or 0.01))
    max_lot = max(base_lot, float(os.getenv("PHOENIX_LOT_MAX_PER_POSITION", "999") or 999.0))
    steps = max(0, math.floor(max(0.0, float(balance) - base_balance) / step_usd))
    return min(max_lot, base_lot + steps * step_lot), steps


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[], help="Profile env file; repeat for overrides")
    parser.add_argument("--session-name", default="", help="Separate Telegram session name for backtests")
    parser.add_argument("--days", type=float, default=6.0)
    parser.add_argument("--sessions", type=int, default=0, help="Use the latest N broker trading dates")
    parser.add_argument("--timeframe", default="M5", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--asset", default="gold", choices=["gold", "nas100", "us30", "btc"])
    parser.add_argument("--symbol", default="", help="Broker symbol override for the selected asset")
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--start-balance", type=float, default=300.0)
    parser.add_argument("--leg-lot", type=float, default=0.0, help="Fixed lot per leg; 0 keeps profile sizing")
    parser.add_argument("--spread-price", type=float, default=-1.0, help="Spread in price units; -1 uses median candle spread")
    parser.add_argument(
        "--broker-offset-hours",
        type=float,
        default=0.0,
        help="Shift Telegram UTC timestamps to the broker candle clock before matching bars",
    )
    parser.add_argument("--extra-market-tp1", action="store_true")
    parser.add_argument(
        "--trade-channels-only",
        action="store_true",
        help="Backtest only channels enabled for execution, not watch-only channels",
    )
    parser.add_argument(
        "--max-edit-delay-minutes",
        type=float,
        default=-1.0,
        help="Exclude parsed signals edited later than this; -1 keeps all edits",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    if args.session_name:
        os.environ["TELEGRAM_SESSION_NAME"] = args.session_name
    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    symbol = ensure_symbol(args.symbol or cfg.symbol)
    end_at = datetime.now(UTC)
    requested_days = max(float(args.days), float(args.sessions) * 2.0 if int(args.sessions) > 0 else 0.0)
    start_at = end_at - timedelta(days=requested_days)
    rates = _rates(symbol, args.timeframe, start_at - timedelta(hours=6), end_at + timedelta(hours=args.horizon_hours + 2))
    if rates.empty:
        raise RuntimeError("No XAUUSD rates returned from MT5")
    session_dates: list[str] = []
    if int(args.sessions) > 0:
        available_dates = sorted({value.date() for value in rates["time"].dt.to_pydatetime()})
        selected_dates = available_dates[-int(args.sessions) :]
        if selected_dates:
            session_dates = [value.isoformat() for value in selected_dates]
            start_at = datetime.combine(selected_dates[0], datetime.min.time(), tzinfo=UTC)
            rates = rates[rates["time"] >= pd.Timestamp(start_at)].reset_index(drop=True)
    runner_symbol_info = mt5.symbol_info(symbol)
    runner_point = float(getattr(runner_symbol_info, "point", 0.01) or 0.01) if runner_symbol_info is not None else 0.01
    runner_median_spread = float(rates["spread"].median()) * runner_point if "spread" in rates.columns else 0.0
    runner_spread_price = runner_median_spread if float(args.spread_price) < 0 else max(0.0, float(args.spread_price))

    configured_channels = cfg.telegram_trade_channels if args.trade_channels_only else cfg.telegram_watch_channels
    wanted = [_variants(item) for item in configured_channels]
    parsed_count = 0
    gold_count = 0
    skipped_assets: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}
    skipped_late_edited_signals = 0
    leg_results: list[LegResult] = []

    client = TelegramClient(str((cfg.data_dir / cfg.telegram_session_name).resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        async for dialog in client.iter_dialogs():
            if not any(_dialog_variants(dialog) & item for item in wanted):
                continue
            channel = str(getattr(dialog.entity, "username", "") or getattr(dialog, "id", ""))
            title = str(getattr(dialog, "title", "") or channel)
            async for message in client.iter_messages(dialog.entity):
                message_date = getattr(message, "date", None)
                if message_date and message_date < start_at:
                    break
                text = str(getattr(message, "raw_text", "") or "")
                parsed = _parse_signal(
                    text,
                    f"{getattr(dialog, 'id', '')}:{getattr(message, 'id', '')}",
                    int(getattr(dialog, "id", 0) or 0),
                    title,
                    "",
                    int(getattr(message, "id", 0) or 0),
                )
                if parsed is None:
                    continue
                parsed_count += 1
                edit_date = getattr(message, "edit_date", None)
                if (
                    float(args.max_edit_delay_minutes) >= 0
                    and message_date is not None
                    and edit_date is not None
                    and (edit_date - message_date).total_seconds() > float(args.max_edit_delay_minutes) * 60.0
                ):
                    skipped_late_edited_signals += 1
                    continue
                if parsed.asset != args.asset:
                    skipped_assets[parsed.asset] = skipped_assets.get(parsed.asset, 0) + 1
                    continue
                gold_count += 1
                broker_message_time = pd.Timestamp(message_date) + pd.Timedelta(hours=float(args.broker_offset_hours))
                start_idx = int(rates["time"].searchsorted(broker_message_time, side="left"))
                if start_idx >= len(rates):
                    continue
                market_price = float(rates.iloc[start_idx]["close"])
                for entry, is_pending, order_kind, target_index, protect_mode, plan_index in _signal_plan(
                    parsed,
                    market_price,
                    add_test_market_tp1=bool(args.extra_market_tp1),
                    runner_spread_price=runner_spread_price,
                ):
                    result = _simulate_leg(
                        parsed,
                        symbol,
                        float(entry),
                        is_pending,
                        order_kind,
                        int(target_index),
                        protect_mode,
                        start_idx,
                        rates,
                        cfg,
                        int(args.horizon_hours),
                    )
                    if result["status"] in {"skipped", "expired"}:
                        key = str(result.get("reason") or result["status"])
                        skip_reasons[key] = skip_reasons.get(key, 0) + 1
                        continue
                    exit_idx = int(result["exit_idx"])
                    leg_results.append(
                        LegResult(
                            signal_time=message_date,
                            entry_time=rates.iloc[int(result["entry_idx"])]["time"].to_pydatetime(),
                            exit_time=rates.iloc[exit_idx]["time"].to_pydatetime(),
                            channel=channel,
                            chat_id=int(getattr(dialog, "id", 0) or 0),
                            title=title,
                            message_id=int(getattr(message, "id", 0) or 0),
                            side=parsed.side,
                            entry=float(result["entry"]),
                            initial_sl=float(result["initial_sl"]),
                            exit=float(result["exit"]),
                            target_index=int(result["target_index"]),
                            plan_index=int(plan_index),
                            order_kind=str(order_kind),
                            protect_mode=protect_mode,
                            status=str(result["status"]),
                            profit_001=_profit(symbol, parsed.side, 0.01, float(result["entry"]), float(result["exit"])),
                            lot_override=_channel_lot_override(cfg, parsed, channel),
                        )
                    )
    finally:
        await client.disconnect()

    balance = float(args.start_balance)
    peak = balance
    max_dd = 0.0
    max_concurrent_lot = 0.0
    max_concurrent_positions = 0
    max_leg_lot = 0.0
    spread_paid = 0.0
    equity: list[dict[str, Any]] = []
    by_channel: dict[str, dict[str, Any]] = {}
    lot_max = max(float(cfg.max_lot), float(cfg.signal_dynamic_lot_max))
    info = None
    try:
        info = mt5.symbol_info(symbol)
        volume_min = float(getattr(info, "volume_min", cfg.min_lot) or cfg.min_lot)
    except Exception:
        volume_min = float(cfg.min_lot)
    point = float(getattr(info, "point", 0.01) or 0.01) if info is not None else 0.01
    median_spread_price = float(rates["spread"].median()) * point if "spread" in rates.columns else 0.0
    spread_price = median_spread_price if float(args.spread_price) < 0 else max(0.0, float(args.spread_price))

    def lot_at_entry(result: LegResult) -> tuple[float, str]:
        if float(args.leg_lot) > 0:
            requested_lot = float(args.leg_lot)
            lot_source = "fixed_backtest_override"
        elif result.plan_index == 999 and str(os.getenv("SIGNAL_EXTRA_MARKET_TP1_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}:
            requested_lot = float(os.getenv("SIGNAL_EXTRA_MARKET_TP1_LOT", str(cfg.signal_fixed_lot)) or cfg.signal_fixed_lot)
            lot_source = "extra_market_tp1"
        elif result.lot_override is not None:
            requested_lot = float(result.lot_override)
            lot_source = "channel_override"
        elif (
            _is_phoenix_source(result.chat_id, result.title)
            and _env_bool("PHOENIX_BALANCE_LOT_SCALING_ENABLED", False)
        ):
            requested_lot, _phoenix_steps = _phoenix_lot_for_balance(balance)
            lot_source = "phoenix_balance_dynamic"
        elif bool(cfg.signal_dynamic_lot_enabled) and str(cfg.signal_lot_mode).lower() == "profit_dynamic":
            requested_lot = _lot_for_net(
                balance - float(args.start_balance),
                float(cfg.signal_fixed_lot),
                float(cfg.signal_dynamic_lot_step_usd),
                float(cfg.signal_dynamic_lot_add),
                lot_max,
            )
            lot_source = "profit_dynamic"
        elif bool(cfg.signal_dynamic_lot_enabled):
            requested_lot = _lot_for_balance(
                balance,
                float(cfg.signal_fixed_lot),
                float(cfg.signal_dynamic_lot_step_usd),
                float(cfg.signal_dynamic_lot_add),
                lot_max,
            )
            lot_source = "balance_dynamic"
        else:
            requested_lot = float(cfg.signal_fixed_lot)
            lot_source = "profile_fixed"
        return normalize_volume(symbol, requested_lot, float(cfg.min_lot), lot_max), lot_source

    timeline: list[tuple[datetime, int, int, LegResult]] = []
    for sequence, result in enumerate(leg_results):
        timeline.append((result.entry_time, 0, sequence, result))
        timeline.append((result.exit_time, 1, sequence, result))
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))
    open_legs: dict[int, tuple[LegResult, float, str, float]] = {}

    for _moment, event_type, sequence, result in timeline:
        if event_type == 0:
            leg_lot, lot_source = lot_at_entry(result)
            spread_cost = abs(_profit(symbol, "buy", leg_lot, result.entry, result.entry + spread_price)) if spread_price > 0 else 0.0
            open_legs[sequence] = (result, leg_lot, lot_source, spread_cost)
            max_leg_lot = max(max_leg_lot, leg_lot, volume_min)
            max_concurrent_positions = max(max_concurrent_positions, len(open_legs))
            max_concurrent_lot = max(
                max_concurrent_lot,
                sum(float(item[1]) for item in open_legs.values()),
            )
            continue

        opened = open_legs.pop(sequence, None)
        if opened is None:
            continue
        result, leg_lot, lot_source, spread_cost = opened
        profit = (result.profit_001 * (leg_lot / 0.01)) - spread_cost
        balance += profit
        spread_paid += spread_cost
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
        row = by_channel.setdefault(result.title or result.channel, {"legs": 0, "wins": 0, "losses": 0, "timeouts": 0, "profit": 0.0})
        row["legs"] += 1
        row["wins"] += 1 if result.status == "win" else 0
        row["losses"] += 1 if result.status == "loss" else 0
        row["timeouts"] += 1 if result.status == "timeout" else 0
        row["profit"] += profit
        equity.append(
            {
                "signal_time": result.signal_time.isoformat(),
                "entry_time": result.entry_time.isoformat(),
                "exit_time": result.exit_time.isoformat(),
                "channel": result.title or result.channel,
                "chat_id": result.chat_id,
                "message_id": result.message_id,
                "status": result.status,
                "side": result.side,
                "entry": round(result.entry, 2),
                "initial_sl": round(result.initial_sl, 2),
                "exit": round(result.exit, 2),
                "target_index": result.target_index,
                "plan_index": result.plan_index,
                "order_kind": result.order_kind,
                "lot_source": lot_source,
                "profit_001": round(result.profit_001, 4),
                "leg_lot": round(leg_lot, 2),
                "spread_cost": round(spread_cost, 2),
                "profit": round(profit, 2),
                "balance": round(balance, 2),
            }
        )

    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "sessions_requested": int(args.sessions),
        "sessions_included": session_dates,
        "symbol": symbol,
        "asset": args.asset,
        "timeframe": args.timeframe,
        "start_balance": round(float(args.start_balance), 2),
        "strategy": "current listener strategy plus one test market TP1 leg" if args.extra_market_tp1 else "current listener strategy",
        "channel_scope": "trade_channels" if args.trade_channels_only else "watch_channels",
        "fixed_leg_lot": round(float(args.leg_lot), 2) if float(args.leg_lot) > 0 else None,
        "spread_price": round(spread_price, 5),
        "spread_paid": round(spread_paid, 2),
        "parsed_signals_all_assets": parsed_count,
        "max_edit_delay_minutes": float(args.max_edit_delay_minutes),
        "skipped_late_edited_signals": skipped_late_edited_signals,
        "selected_asset_signals": gold_count,
        "gold_signals": gold_count if args.asset == "gold" else 0,
        "skipped_assets": skipped_assets,
        "closed_or_timed_out_legs": len(leg_results),
        "skip_reasons": skip_reasons,
        "final_balance": round(balance, 2),
        "profit": round(balance - float(args.start_balance), 2),
        "profit_percent": round(((balance / float(args.start_balance)) - 1.0) * 100.0, 2),
        "max_drawdown_from_peak": round(max_dd, 2),
        "max_position_lot_requested": round(max_leg_lot, 2),
        "max_leg_lot_used": round(max_leg_lot, 2),
        "max_concurrent_positions": int(max_concurrent_positions),
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "next_position_lot": (
            round(float(args.leg_lot), 2)
            if float(args.leg_lot) > 0
            else round(_phoenix_lot_for_balance(balance)[0], 2)
            if _env_bool("PHOENIX_BALANCE_LOT_SCALING_ENABLED", False)
            else round(
                _lot_for_net(
                    balance - float(args.start_balance),
                    float(cfg.signal_fixed_lot),
                    float(cfg.signal_dynamic_lot_step_usd),
                    float(cfg.signal_dynamic_lot_add),
                    lot_max,
                )
                if str(cfg.signal_lot_mode).lower() == "profit_dynamic"
                else _lot_for_balance(
                    balance,
                    float(cfg.signal_fixed_lot),
                    float(cfg.signal_dynamic_lot_step_usd),
                    float(cfg.signal_dynamic_lot_add),
                    lot_max,
                ),
                2,
            )
        ),
        "lot_rule": {
            "mode": str(cfg.signal_lot_mode),
            "base_leg_lot": float(cfg.signal_fixed_lot),
            "profit_step_usd": float(cfg.signal_dynamic_lot_step_usd),
            "add_per_step": float(cfg.signal_dynamic_lot_add),
            "extra_market_tp1_lot": float(os.getenv("SIGNAL_EXTRA_MARKET_TP1_LOT", "0") or 0.0),
            "channel_overrides": dict(cfg.channel_lot_sizes),
            "phoenix_balance_scaling": {
                "enabled": _env_bool("PHOENIX_BALANCE_LOT_SCALING_ENABLED", False),
                "base_balance": float(os.getenv("PHOENIX_LOT_BASE_BALANCE_USD", "1000") or 1000.0),
                "base_leg_lot": float(os.getenv("PHOENIX_LOT_BASE_PER_POSITION", "0.10") or 0.10),
                "balance_step_usd": float(os.getenv("PHOENIX_LOT_BALANCE_STEP_USD", "500") or 500.0),
                "add_per_step": float(os.getenv("PHOENIX_LOT_STEP_ADD", "0.01") or 0.01),
                "max_leg_lot": float(os.getenv("PHOENIX_LOT_MAX_PER_POSITION", "999") or 999.0),
            },
        },
        "by_channel": {
            key: {
                "legs": value["legs"],
                "wins": value["wins"],
                "losses": value["losses"],
                "timeouts": value["timeouts"],
                "profit": round(value["profit"], 2),
            }
            for key, value in sorted(by_channel.items(), key=lambda item: item[1]["profit"], reverse=True)
        },
        "equity": equity,
    }

    path = Path(args.output) if args.output else cfg.data_dir / f"current_three_leg_projection_{int(args.days)}d_{args.timeframe.lower()}_start300.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
