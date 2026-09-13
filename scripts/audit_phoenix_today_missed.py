from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _parse_signal


PHOENIX_ID = -1002864291293


def _middle_entry(entries: list[float]) -> float:
    levels = sorted({round(float(value), 3) for value in entries if float(value or 0.0) > 0.0})
    if len(levels) == 2:
        levels.insert(1, round((levels[0] + levels[1]) / 2.0, 3))
    return levels[len(levels) // 2]


def _capped_sl(side: str, entry: float, provider_sl: float, cap: float) -> float:
    if side == "buy":
        return max(provider_sl, entry - cap)
    return min(provider_sl, entry + cap)


def _pending_trigger_index(
    side: str,
    entry: float,
    rates: pd.DataFrame,
    start: int,
    end: int,
    point: float,
) -> int:
    for index in range(start, end):
        row = rates.iloc[index]
        spread_price = float(row.get("spread", 0.0) or 0.0) * point
        if side == "buy":
            if float(row["low"]) + spread_price <= entry <= float(row["high"]) + spread_price:
                return index
        elif float(row["low"]) <= entry <= float(row["high"]):
            return index
    return -1


def _market_entry_price(side: str, row: pd.Series, point: float) -> float:
    spread_price = float(row.get("spread", 0.0) or 0.0) * point
    open_price = float(row["open"])
    return open_price + spread_price if side == "buy" else open_price


def _market_runner_allowed(
    side: str,
    entries: list[float],
    market_price: float,
    tps: list[float],
    tolerance: float,
    min_tp1_distance: float,
) -> bool:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0.0]
    if not clean_entries or not tps or market_price <= 0.0:
        return False
    live_tps = [float(tp) for tp in tps if (float(tp) > market_price if side == "buy" else float(tp) < market_price)]
    if not live_tps:
        return False
    if not (min(clean_entries) - tolerance <= market_price <= max(clean_entries) + tolerance):
        return False
    return abs(live_tps[0] - market_price) >= min_tp1_distance


def _simulate_leg(
    side: str,
    entry: float,
    sl: float,
    tp: float,
    rates: pd.DataFrame,
    start: int,
    end: int,
    lot: float,
    symbol: str,
    commission_per_001: float,
) -> dict[str, Any]:
    exit_price = float(rates.iloc[end - 1]["close"])
    status = "open_at_end"
    for index in range(start, end):
        row = rates.iloc[index]
        low = float(row["low"])
        high = float(row["high"])
        sl_hit = low <= sl if side == "buy" else high >= sl
        tp_hit = high >= tp if side == "buy" else low <= tp
        if sl_hit:
            exit_price = sl
            status = "sl"
            break
        if tp_hit:
            exit_price = tp
            status = "tp"
            break
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    gross = float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)
    commission = commission_per_001 * (lot / 0.01)
    return {
        "status": status,
        "entry": entry,
        "exit": exit_price,
        "gross_pnl": gross,
        "commission": commission,
        "net_pnl": gross - commission,
    }


def _event_trace(events_path: Path, message_ids: set[int]) -> dict[int, dict[str, Any]]:
    traces: dict[int, dict[str, Any]] = {
        message_id: {"types": Counter(), "errors": Counter(), "successful_orders": 0}
        for message_id in message_ids
    }
    if not events_path.exists():
        return traces
    with events_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not any(f'"message_id": {message_id}' in line for message_id in message_ids):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            signal = row.get("signal") or {}
            message_id = int(signal.get("message_id") or row.get("message_id") or 0)
            if message_id not in traces:
                continue
            traces[message_id]["types"][str(row.get("type") or "unknown")] += 1
            if row.get("error"):
                traces[message_id]["errors"][str(row["error"])] += 1
            if row.get("type") == "order_attempt" and int(row.get("retcode") or 0) in {10008, 10009, 10010}:
                traces[message_id]["successful_orders"] += 1
    return traces


async def _today_signals(cfg, day: datetime.date) -> list[dict[str, Any]]:
    client = TelegramClient(
        str((cfg.data_dir / "xauusd_signal_bot_backtest_copy").resolve()),
        cfg.telegram_api_id,
        cfg.telegram_api_hash,
    )
    await client.connect()
    try:
        dialog = None
        async for candidate in client.iter_dialogs():
            if int(candidate.id) == PHOENIX_ID:
                dialog = candidate
                break
        if dialog is None:
            raise RuntimeError("Phoenix dialog not found")
        rows: list[dict[str, Any]] = []
        async for message in client.iter_messages(dialog.entity):
            if message.date.date() < day:
                break
            if message.date.date() != day:
                continue
            signal = _parse_signal(
                str(message.raw_text or ""),
                f"{PHOENIX_ID}:{message.id}",
                PHOENIX_ID,
                str(dialog.title or "PHOENIX VIP"),
                str(getattr(message, "post_author", "") or ""),
                int(message.id),
            )
            if signal is None or signal.asset != "gold" or len(signal.tps) < 1:
                continue
            rows.append({"message": message, "signal": signal})
        return sorted(rows, key=lambda row: row["message"].date)
    finally:
        await client.disconnect()


async def main() -> None:
    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(ROOT / env_file, override=True)
    cfg = load_settings()
    day = datetime.now(UTC).date()
    lot = float(__import__("os").getenv("PHOENIX_LOT_BASE_PER_POSITION", "0.10") or 0.10)
    balance = 0.0
    step_usd = float(__import__("os").getenv("PHOENIX_LOT_BALANCE_STEP_USD", "300") or 300.0)
    step_add = float(__import__("os").getenv("PHOENIX_LOT_STEP_ADD", "0.01") or 0.01)
    sl_cap = float(__import__("os").getenv("PHOENIX_MAX_STOP_DISTANCE_USD", "8") or 8.0)
    pending_minutes = int(float(__import__("os").getenv("PHOENIX_PENDING_EXPIRY_MINUTES", "60") or 60.0))
    commission_per_001 = 0.06
    broker_offset = timedelta(hours=3)
    market_tolerance = float(__import__("os").getenv("PHOENIX_MARKET_ENTRY_TOLERANCE", "2") or 2.0)
    market_min_tp1_distance = float(
        __import__("os").getenv("PHOENIX_MIN_MARKET_TP1_DISTANCE", "0.5") or 0.5
    )

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        account = mt5.account_info()
        balance = float(getattr(account, "balance", 0.0) or 0.0)
        if str(__import__("os").getenv("PHOENIX_BALANCE_LOT_SCALING_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}:
            base_balance = float(__import__("os").getenv("PHOENIX_LOT_BASE_BALANCE_USD", "0") or 0.0)
            steps = max(0, int((balance - base_balance) // step_usd))
            lot += steps * step_add
        lot = round(lot, 2)

        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        end = datetime.now(UTC) + timedelta(minutes=2)
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"No M1 rates: {mt5.last_error()}")
        rates = pd.DataFrame(raw)
        rates["time"] = pd.to_datetime(rates["time"], unit="s", utc=True)
        rates = rates.sort_values("time").reset_index(drop=True)
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)

        signals = await _today_signals(cfg, day)
        message_ids = {int(row["message"].id) for row in signals}
        traces = _event_trace(cfg.data_dir / "telegram_signal_events.jsonl", message_ids)
        results: list[dict[str, Any]] = []
        for item in signals:
            message = item["message"]
            signal = item["signal"]
            entry = _middle_entry(signal.entries)
            sl = _capped_sl(signal.side, entry, float(signal.sl), sl_cap)
            broker_message_time = pd.Timestamp(message.date + broker_offset)
            start_index = int(rates["time"].searchsorted(broker_message_time, side="left"))
            expiry = broker_message_time + pd.Timedelta(minutes=pending_minutes)
            expiry_index = min(len(rates), int(rates["time"].searchsorted(expiry, side="right")))
            trigger_index = _pending_trigger_index(signal.side, entry, rates, start_index, expiry_index, point)
            market_runner: dict[str, Any] | None = None
            if start_index < len(rates):
                market_entry = _market_entry_price(signal.side, rates.iloc[start_index], point)
                if _market_runner_allowed(
                    signal.side,
                    signal.entries,
                    market_entry,
                    signal.tps,
                    market_tolerance,
                    market_min_tp1_distance,
                ):
                    live_tp = next(
                        float(tp)
                        for tp in signal.tps
                        if (float(tp) > market_entry if signal.side == "buy" else float(tp) < market_entry)
                    )
                    market_sl = _capped_sl(signal.side, market_entry, float(signal.sl), sl_cap)
                    market_runner = _simulate_leg(
                        signal.side,
                        market_entry,
                        market_sl,
                        live_tp,
                        rates,
                        start_index,
                        len(rates),
                        lot,
                        symbol,
                        commission_per_001,
                    )
                    market_runner["tp"] = live_tp
                    market_runner["sl"] = market_sl
            legs: list[dict[str, Any]] = []
            if trigger_index >= 0:
                for target_index, tp in enumerate(signal.tps[:6], start=1):
                    leg = _simulate_leg(
                        signal.side,
                        entry,
                        sl,
                        float(tp),
                        rates,
                        trigger_index,
                        len(rates),
                        lot,
                        symbol,
                        commission_per_001,
                    )
                    leg["target_index"] = target_index
                    leg["tp"] = float(tp)
                    legs.append(leg)
            trace = traces[int(message.id)]
            results.append(
                {
                    "message_id": int(message.id),
                    "time_utc": message.date.isoformat(),
                    "broker_candle_time": broker_message_time.isoformat(),
                    "side": signal.side,
                    "entries": signal.entries,
                    "middle_entry": entry,
                    "provider_sl": float(signal.sl),
                    "effective_sl": sl,
                    "tps_used": [float(value) for value in signal.tps[:6]],
                    "pending_triggered": trigger_index >= 0,
                    "trigger_time_utc": rates.iloc[trigger_index]["time"].isoformat() if trigger_index >= 0 else "",
                    "listener_event_types": dict(trace["types"]),
                    "listener_errors": dict(trace["errors"]),
                    "successful_orders": int(trace["successful_orders"]),
                    "legs": legs,
                    "market_runner": market_runner,
                    "potential_core_net_pnl": round(sum(float(leg["net_pnl"]) for leg in legs), 2),
                    "potential_net_pnl": round(
                        sum(float(leg["net_pnl"]) for leg in legs)
                        + (float(market_runner["net_pnl"]) if market_runner else 0.0),
                        2,
                    ),
                }
            )

        report = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "day_utc": day.isoformat(),
            "symbol": symbol,
            "account_balance": balance,
            "lot_per_leg": lot,
            "legs_per_signal": 6,
            "entry_mapping": "middle",
            "pending_expiry_minutes": pending_minutes,
            "sl_cap_usd": sl_cap,
            "commission_per_001": commission_per_001,
            "broker_candle_offset_hours": 3,
            "signals": results,
            "totals": {
                "full_signals": len(results),
                "signals_with_successful_orders": sum(1 for row in results if row["successful_orders"] > 0),
                "missed_signals": sum(1 for row in results if row["successful_orders"] == 0),
                "historically_triggered_middle_entries": sum(1 for row in results if row["pending_triggered"]),
                "eligible_market_runners": sum(1 for row in results if row["market_runner"] is not None),
                "potential_core_net_pnl": round(sum(float(row["potential_core_net_pnl"]) for row in results), 2),
                "potential_net_pnl": round(sum(float(row["potential_net_pnl"]) for row in results), 2),
            },
            "limitations": [
                "M1 simulation uses conservative SL-first ordering when SL and TP occur in one candle.",
                "Potential PnL covers the configured six middle-entry legs and eligible extra MARKET TP1 runner.",
                "The preliminary direction runner fired from an earlier partial post and remains excluded.",
                "Open-at-end legs are marked to market at the final available M1 close.",
            ],
        }
        output = cfg.data_dir / f"phoenix_today_missed_{day.isoformat()}.json"
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(output), "totals": report["totals"], "signals": results}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
