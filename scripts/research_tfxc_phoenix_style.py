from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from telethon import TelegramClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _parse_signal


CHANNEL_ID = -1001220837618
ROUND_TRIP_COST_PER_001 = 0.17


@dataclass(frozen=True)
class SignalRow:
    message_id: int
    time_utc: datetime
    side: str
    entry: float
    sl: float
    tps: tuple[float, ...]


@dataclass(frozen=True)
class Variant:
    name: str
    targets: tuple[int, ...]
    sl_cap_usd: float
    be_after_tp: int
    entry_mode: str = "exact_touch"
    be_buffer_usd: float = 0.05


def _profit(side: str, entry: float, exit_price: float) -> float:
    direction = 1.0 if side == "buy" else -1.0
    return ((exit_price - entry) * direction) - ROUND_TRIP_COST_PER_001


def _effective_sl(signal: SignalRow, cap: float) -> float:
    if cap <= 0:
        return signal.sl
    if signal.side == "buy":
        return max(signal.sl, signal.entry - cap)
    return min(signal.sl, signal.entry + cap)


def _entry_bar_index(signal: SignalRow, rates: pd.DataFrame, max_wait_minutes: int) -> int:
    times = rates["time"]
    start = int(times.searchsorted(pd.Timestamp(signal.time_utc), side="left"))
    if start >= len(rates):
        return -1
    end_time = pd.Timestamp(signal.time_utc) + pd.Timedelta(minutes=max_wait_minutes)
    end = min(len(rates), int(times.searchsorted(end_time, side="right")))
    for index in range(start, max(start + 1, end)):
        row = rates.iloc[index]
        if float(row["low"]) <= signal.entry <= float(row["high"]):
            return index
    return -1


def _first_market_bar_index(signal: SignalRow, rates: pd.DataFrame) -> int:
    index = int(rates["time"].searchsorted(pd.Timestamp(signal.time_utc), side="left"))
    return index if 0 <= index < len(rates) else -1


def _market_shifted_signal(signal: SignalRow, market_entry: float) -> SignalRow:
    direction = 1.0 if signal.side == "buy" else -1.0
    stop_distance = abs(signal.entry - signal.sl)
    target_distances = tuple(abs(target - signal.entry) for target in signal.tps)
    return SignalRow(
        message_id=signal.message_id,
        time_utc=signal.time_utc,
        side=signal.side,
        entry=market_entry,
        sl=market_entry - (direction * stop_distance),
        tps=tuple(market_entry + (direction * distance) for distance in target_distances),
    )


def _levels_valid(signal: SignalRow, targets: tuple[int, ...]) -> bool:
    direction = 1.0 if signal.side == "buy" else -1.0
    if (signal.entry - signal.sl) * direction <= 0:
        return False
    return all((signal.tps[index - 1] - signal.entry) * direction > 0 for index in targets)


def _simulate_leg(
    signal: SignalRow,
    rates: pd.DataFrame,
    entry_index: int,
    target_index: int,
    sl: float,
    be_after_tp: int,
    be_buffer_usd: float,
    horizon_hours: int,
) -> dict[str, Any]:
    target = signal.tps[target_index - 1]
    be_trigger = signal.tps[be_after_tp - 1] if 0 < be_after_tp <= len(signal.tps) else 0.0
    stop = sl
    be_armed = False
    times = rates["time"]
    end_time = pd.Timestamp(signal.time_utc) + pd.Timedelta(hours=horizon_hours)
    end = min(len(rates), int(times.searchsorted(end_time, side="right")))
    exit_price = float(rates.iloc[max(entry_index, end - 1)]["close"])
    status = "timeout"

    for index in range(entry_index, max(entry_index + 1, end)):
        row = rates.iloc[index]
        high = float(row["high"])
        low = float(row["low"])
        stop_hit = low <= stop if signal.side == "buy" else high >= stop
        target_hit = high >= target if signal.side == "buy" else low <= target
        be_hit = (
            be_trigger > 0
            and (high >= be_trigger if signal.side == "buy" else low <= be_trigger)
        )

        # Pessimistic M1 tie handling: protection/SL is counted before TP.
        if stop_hit:
            exit_price = stop
            status = "be" if be_armed else "sl"
            break
        if target_hit:
            exit_price = target
            status = "tp"
            break
        if be_hit and target_index > be_after_tp:
            stop = signal.entry + be_buffer_usd if signal.side == "buy" else signal.entry - be_buffer_usd
            be_armed = True

    return {
        "status": status,
        "target_index": target_index,
        "entry": signal.entry,
        "exit": exit_price,
        "net_pnl_001": _profit(signal.side, signal.entry, exit_price),
    }


def _max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def _summarize(name: str, trades: list[dict[str, Any]], signals: int, no_entry: int) -> dict[str, Any]:
    pnl = [float(row["net_pnl_001"]) for row in trades]
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = abs(sum(value for value in pnl if value < 0))
    winning_signals = len({row["message_id"] for row in trades if row["signal_pnl_001"] > 0})
    return {
        "name": name,
        "signals": signals,
        "no_entry": no_entry,
        "legs": len(trades),
        "winning_signals": winning_signals,
        "signal_win_rate_pct": round(100.0 * winning_signals / max(1, signals - no_entry), 2),
        "net_pnl_001": round(sum(pnl), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown_001": round(_max_drawdown(pnl), 2),
        "average_leg_001": round(sum(pnl) / max(1, len(pnl)), 4),
        "outcomes": {
            status: sum(1 for row in trades if row["status"] == status)
            for status in ("tp", "sl", "be", "timeout")
        },
    }


async def _load_signals(client: TelegramClient, cutoff: datetime) -> tuple[str, list[SignalRow], int]:
    dialog = None
    async for candidate in client.iter_dialogs():
        if int(candidate.id) == CHANNEL_ID:
            dialog = candidate
            break
    if dialog is None:
        raise RuntimeError(f"Telegram channel not found: {CHANNEL_ID}")

    rows: list[SignalRow] = []
    parsed_gold = 0
    recent_signatures: dict[tuple[Any, ...], datetime] = {}
    async for message in client.iter_messages(dialog.entity):
        if message.date and message.date < cutoff:
            break
        parsed = _parse_signal(
            str(message.raw_text or ""),
            f"{CHANNEL_ID}:{message.id}",
            CHANNEL_ID,
            str(dialog.title or ""),
            str(getattr(message, "post_author", "") or ""),
            int(message.id),
        )
        if parsed is None or parsed.asset != "gold" or not parsed.entry or not parsed.sl or not parsed.tps:
            continue
        parsed_gold += 1
        signature = (
            parsed.side,
            round(float(parsed.entry), 2),
            round(float(parsed.sl), 2),
            tuple(round(float(value), 2) for value in parsed.tps),
        )
        previous = recent_signatures.get(signature)
        if previous is not None and abs((message.date - previous).total_seconds()) <= 900:
            continue
        recent_signatures[signature] = message.date
        rows.append(
            SignalRow(
                message_id=int(message.id),
                time_utc=message.date,
                side=str(parsed.side),
                entry=float(parsed.entry),
                sl=float(parsed.sl),
                tps=tuple(float(value) for value in parsed.tps),
            )
        )
    rows.sort(key=lambda row: row.time_utc)
    return str(dialog.title or CHANNEL_ID), rows, parsed_gold


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--entry-wait-minutes", type=int, default=5)
    parser.add_argument("--broker-offset-hours", type=float, default=3.0)
    parser.add_argument("--session-name", default="xauusd_signal_bot_backtest_copy")
    parser.add_argument("--output", default="data_vantage/tfxc_phoenix_style_90d_m1.json")
    args = parser.parse_args()

    cfg = load_settings()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=args.days)
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        raw = mt5.copy_rates_range(
            symbol,
            mt5.TIMEFRAME_M1,
            cutoff - timedelta(days=2),
            now + timedelta(hours=1),
        )
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"No M1 rates for {symbol}: {mt5.last_error()}")
        rates = pd.DataFrame(raw)
        rates["time"] = pd.to_datetime(rates["time"], unit="s", utc=True)
        rates = rates.sort_values("time").reset_index(drop=True)

        client = TelegramClient(
            str((cfg.data_dir / args.session_name).resolve()),
            cfg.telegram_api_id,
            cfg.telegram_api_hash,
        )
        await client.connect()
        try:
            title, signals, parsed_gold = await _load_signals(client, cutoff)
        finally:
            await client.disconnect()
        signals = [
            replace(row, time_utc=row.time_utc + timedelta(hours=float(args.broker_offset_hours)))
            for row in signals
        ]

        variants = [
            Variant("exact_tp1_provider_sl", (1,), 0.0, 0),
            Variant("exact_tp1_tp2_provider_sl", (1, 2), 0.0, 0),
            Variant("exact_phoenix_3leg_provider_sl_no_be", (1, 2, 3), 0.0, 0),
            Variant("shifted_tp1_provider_sl", (1,), 0.0, 0, "market_shifted"),
            Variant("shifted_tp1_tp2_provider_sl", (1, 2), 0.0, 0, "market_shifted"),
        ]
        for cap in (6.0, 8.0, 10.0, 12.0, 0.0):
            cap_name = "provider" if cap <= 0 else f"cap{cap:g}"
            for be_after in (0, 1, 2):
                be_name = "no_be" if be_after == 0 else f"be_after_tp{be_after}"
                variants.append(
                    Variant(f"exact_phoenix_3leg_{cap_name}_{be_name}", (1, 2, 3), cap, be_after)
                )
                variants.append(
                    Variant(
                        f"shifted_phoenix_3leg_{cap_name}_{be_name}",
                        (1, 2, 3),
                        cap,
                        be_after,
                        "market_shifted",
                    )
                )

        results: list[dict[str, Any]] = []
        for variant in variants:
            trades: list[dict[str, Any]] = []
            no_entry = 0
            for signal in signals:
                if len(signal.tps) < max(variant.targets):
                    no_entry += 1
                    continue
                if variant.entry_mode == "market_shifted":
                    entry_index = _first_market_bar_index(signal, rates)
                    execution_signal = (
                        _market_shifted_signal(signal, float(rates.iloc[entry_index]["open"]))
                        if entry_index >= 0
                        else signal
                    )
                else:
                    entry_index = _entry_bar_index(signal, rates, args.entry_wait_minutes)
                    execution_signal = signal
                if entry_index < 0:
                    no_entry += 1
                    continue
                if not _levels_valid(execution_signal, variant.targets):
                    no_entry += 1
                    continue
                sl = _effective_sl(execution_signal, variant.sl_cap_usd)
                signal_trades = []
                for target_index in variant.targets:
                    leg = _simulate_leg(
                        execution_signal,
                        rates,
                        entry_index,
                        target_index,
                        sl,
                        variant.be_after_tp,
                        variant.be_buffer_usd,
                        args.horizon_hours,
                    )
                    leg["message_id"] = signal.message_id
                    leg["time_utc"] = signal.time_utc.isoformat()
                    signal_trades.append(leg)
                signal_pnl = sum(float(row["net_pnl_001"]) for row in signal_trades)
                for leg in signal_trades:
                    leg["signal_pnl_001"] = signal_pnl
                trades.extend(signal_trades)
            summary = _summarize(variant.name, trades, len(signals), no_entry)
            summary["config"] = asdict(variant)
            results.append(summary)

        unique_results = {item["name"]: item for item in results}
        ranked = sorted(unique_results.values(), key=lambda item: (item["net_pnl_001"], item["profit_factor"] or 0), reverse=True)
        report = {
            "generated_utc": now.isoformat(),
            "channel_id": CHANNEL_ID,
            "channel_title": title,
            "period_days": args.days,
            "cutoff_utc": cutoff.isoformat(),
            "rates_start_utc": rates["time"].iloc[0].isoformat(),
            "rates_end_utc": rates["time"].iloc[-1].isoformat(),
            "timeframe": "M1",
            "broker_candle_offset_hours": float(args.broker_offset_hours),
            "parsed_gold_messages": parsed_gold,
            "deduplicated_signals": len(signals),
            "entry_rule": f"posted entry must be touched within {args.entry_wait_minutes} minutes",
            "tie_rule": "pessimistic: SL/BE before TP inside the same M1 candle",
            "cost_per_leg_001_usd": ROUND_TRIP_COST_PER_001,
            "lot_basis": "all PnL values use 0.01 lot per leg",
            "results": ranked,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({"output": str(output), "signals": len(signals), "top": ranked[:8]}, ensure_ascii=True))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
