from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _channel_strategy, _phoenix_progressive_stop
from backtest_current_three_leg_projection import (
    _auto_sl_from_history,
    _better_stop,
    _entry_touched,
    _hit_sl,
    _hit_tp,
    _pending_valid,
    _price_reached,
    _profit,
    _select_live_tps,
    _signal_plan,
)
from calibrate_top25_channels import _dialog_variants, _fetch_channel_signals, _rates, _variants


CONFIGS = {
    "baseline": {},
    "positive_stale_30": {"stale": 30, "require_tp1": False},
    "positive_stale_60": {"stale": 60, "require_tp1": False},
    "positive_stale_90": {"stale": 90, "require_tp1": False},
    "after_tp1_stale_30": {"stale": 30, "require_tp1": True},
    "after_tp1_stale_60": {"stale": 60, "require_tp1": True},
    "after_tp1_stale_90": {"stale": 90, "require_tp1": True},
    "after_tp1_stale_120": {"stale": 120, "require_tp1": True},
    "giveback_25": {"giveback": 0.25},
    "giveback_40": {"giveback": 0.40},
    "giveback_50": {"giveback": 0.50},
    "giveback_65": {"giveback": 0.65},
    "trend_reversal": {"trend": True},
    "compromise_30": {"compromise": True, "stale": 30, "giveback": 0.50},
    "compromise_60": {"compromise": True, "stale": 60, "giveback": 0.50},
    "compromise_90": {"compromise": True, "stale": 90, "giveback": 0.50},
}


def _favorable(side: str, entry: float, price: float) -> float:
    return price - entry if side == "buy" else entry - price


def _trend_opposite(side: str, row: pd.Series) -> bool:
    fast = float(row.get("ema9", 0.0) or 0.0)
    slow = float(row.get("ema21", 0.0) or 0.0)
    return fast < slow if side == "buy" else fast > slow


def _simulate(
    row: Any,
    cfg: Any,
    entry: float,
    pending: bool,
    order_kind: str,
    target_index: int,
    protect_mode: str,
    overlay: dict[str, Any],
) -> dict[str, Any]:
    rates = row.rates
    signal = row.signal
    start_idx = row.start_idx
    market = row.market
    if pending and not _pending_valid(signal.side, order_kind, entry, market):
        return {"status": "skip"}
    try:
        tp1, target, live_index, live_tps = _select_live_tps(signal.side, entry, signal.tps, target_index)
    except Exception:
        return {"status": "skip"}
    if live_index <= 1:
        return {"status": "skip"}
    sl = float(signal.sl or 0.0)
    if sl <= 0:
        sl = _auto_sl_from_history(signal, row.symbol, entry, rates.iloc[start_idx], cfg.signal_sl_atr_mult, cfg.signal_sl_min_points)
    if signal.side == "buy" and not (sl < entry < target):
        return {"status": "skip"}
    if signal.side == "sell" and not (target < entry < sl):
        return {"status": "skip"}

    strategy = _channel_strategy(signal)
    trigger_idx = start_idx
    if pending:
        expiry = rates.iloc[start_idx]["time"] + pd.Timedelta(minutes=float(strategy.pending_expiry_minutes or 15.0))
        expiry_idx = min(int(rates["time"].searchsorted(expiry, side="right")), len(rates))
        trigger_idx = -1
        for idx in range(start_idx, expiry_idx):
            candle = rates.iloc[idx]
            high, low = float(candle["high"]), float(candle["low"])
            if _price_reached(signal.side, high if signal.side == "buy" else low, tp1):
                return {"status": "skip"}
            if _entry_touched(entry, high, low):
                trigger_idx = idx
                break
        if trigger_idx < 0:
            return {"status": "skip"}

    end_idx = min(trigger_idx + int(24 * 60 / 5), len(rates))
    current_sl = sl
    reached = 0
    last_progress_idx = trigger_idx
    mfe = 0.0
    opposite_bars = 0
    for idx in range(trigger_idx, end_idx):
        candle = rates.iloc[idx]
        high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])
        if _hit_sl(signal.side, current_sl, high, low):
            return {"status": "sl", "entry": entry, "exit": current_sl, "entry_idx": trigger_idx, "exit_idx": idx}
        if _hit_tp(signal.side, target, high, low):
            return {"status": "tp", "entry": entry, "exit": target, "entry_idx": trigger_idx, "exit_idx": idx}

        favorable_peak = _favorable(signal.side, entry, high if signal.side == "buy" else low)
        mfe = max(mfe, favorable_peak)
        previous_reached = reached
        for level, tp_value in enumerate(live_tps, start=1):
            if _hit_tp(signal.side, float(tp_value), high, low):
                reached = max(reached, level)
        if reached > previous_reached:
            last_progress_idx = idx

        if protect_mode == "be" and reached >= 1:
            current_sl = _better_stop(signal.side, current_sl, entry)
        elif protect_mode == "tp1" and reached >= 1:
            current_sl = _better_stop(signal.side, current_sl, tp1)
        elif protect_mode == "be_after_tp3" and reached >= 3:
            current_sl = _better_stop(signal.side, current_sl, entry)
        elif protect_mode == "tp1_after_tp3" and reached >= 3:
            current_sl = _better_stop(signal.side, current_sl, tp1)
        elif protect_mode == "phoenix_ladder" and reached >= 1:
            candidate = _phoenix_progressive_stop(signal.side, entry, live_tps, reached, current_sl)
            if candidate is not None:
                current_sl = candidate

        floating = _favorable(signal.side, entry, close)
        if floating <= 0:
            opposite_bars = 0
            continue
        opposite_bars = opposite_bars + 1 if _trend_opposite(signal.side, candle) else 0
        minutes_open = (idx - trigger_idx) * 5
        minutes_stale = (idx - last_progress_idx) * 5
        tp1_seen = reached >= 1
        stale = int(overlay.get("stale", 0) or 0)
        require_tp1 = bool(overlay.get("require_tp1", True))
        stale_hit = bool(stale and minutes_stale >= stale and (tp1_seen or not require_tp1))
        giveback = float(overlay.get("giveback", 0.0) or 0.0)
        giveback_hit = bool(tp1_seen and giveback > 0 and mfe > 0 and floating <= mfe * (1.0 - giveback))
        trend_hit = bool(tp1_seen and overlay.get("trend") and opposite_bars >= 2)
        compromise_hit = bool(
            overlay.get("compromise")
            and tp1_seen
            and opposite_bars >= 2
            and (stale_hit or giveback_hit)
        )
        if stale_hit or giveback_hit or trend_hit or compromise_hit:
            if overlay.get("compromise") and not compromise_hit:
                continue
            return {
                "status": "managed",
                "entry": entry,
                "exit": close,
                "entry_idx": trigger_idx,
                "exit_idx": idx,
                "minutes_open": minutes_open,
                "reached": reached,
            }

    exit_idx = max(trigger_idx, end_idx - 1)
    return {"status": "timeout", "entry": entry, "exit": float(rates.iloc[exit_idx]["close"]), "entry_idx": trigger_idx, "exit_idx": exit_idx}


def _score(events: list[dict[str, Any]]) -> dict[str, Any]:
    events = sorted(events, key=lambda item: item["exit_time"])
    pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    wins = losses = managed = sl_hits = 0
    hold_minutes = []
    for event in events:
        value = float(event["pnl"])
        pnl += value
        peak = max(peak, pnl)
        max_dd = min(max_dd, pnl - peak)
        wins += int(value > 0.01)
        losses += int(value < -0.01)
        managed += int(event["status"] == "managed")
        sl_hits += int(event["status"] == "sl")
        hold_minutes.append(float(event["hold_minutes"]))
    return {
        "legs": len(events),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "managed_exits": managed,
        "sl_hits": sl_hits,
        "pnl_001": round(pnl, 2),
        "max_dd_001": round(max_dd, 2),
        "avg_hold_minutes": round(sum(hold_minutes) / max(1, len(hold_minutes)), 1),
    }


async def main() -> None:
    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(ROOT / env_file, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        end = datetime.now(UTC)
        start = end - timedelta(days=126)
        offset = timedelta(minutes=180)
        candidates = {"gold": cfg.symbol, "nas100": "NAS100", "us30": "DJ30"}
        rates_by_asset = {}
        for asset, candidate in candidates.items():
            try:
                symbol = ensure_symbol(candidate)
                rates = _rates(symbol, "M5", start + offset - timedelta(days=2), end + offset + timedelta(days=2))
                rates["ema9"] = rates["close"].ewm(span=9, adjust=False).mean()
                rates["ema21"] = rates["close"].ewm(span=21, adjust=False).mean()
                info = mt5.symbol_info(symbol)
                point = float(getattr(info, "point", 0.01) or 0.01)
                spread = float(rates["spread"].median()) * point
                rates_by_asset[asset] = {"symbol": symbol, "rates": rates, "spread_price": spread}
            except Exception as exc:
                print(f"skip {asset}: {exc}", flush=True)

        source = cfg.data_dir / f"{cfg.telegram_session_name}.session"
        copy = cfg.data_dir / f"{cfg.telegram_session_name}_trend90.session"
        if source.exists():
            shutil.copy2(source, copy)
        client = TelegramClient(str(copy.with_suffix("").resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
        await client.connect()
        try:
            wanted = [_variants(item) for item in cfg.telegram_trade_channels]
            dialogs = [dialog async for dialog in client.iter_dialogs() if any(_dialog_variants(dialog) & token for token in wanted)]
            rows = []
            for index, dialog in enumerate(dialogs, start=1):
                fetched = await _fetch_channel_signals(client, dialog, start, rates_by_asset, 180)
                rows.extend(fetched)
                title = str(getattr(dialog, "title", dialog.id)).encode("ascii", "ignore").decode("ascii") or str(dialog.id)
                print(f"{index}/{len(dialogs)} {title} signals={len(fetched)}", flush=True)
        finally:
            await client.disconnect()

        events_by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            plan = _signal_plan(
                row.signal,
                row.market,
                add_test_market_tp1=False,
                runner_spread_price=row.spread_price,
            )
            for entry, pending, order_kind, target_index, protect_mode, _plan_index in plan:
                if int(target_index) <= 1:
                    continue
                for name, overlay in CONFIGS.items():
                    result = _simulate(row, cfg, float(entry), bool(pending), str(order_kind), int(target_index), str(protect_mode), overlay)
                    if result.get("status") == "skip":
                        continue
                    entry_price = float(result["entry"])
                    exit_price = float(result["exit"])
                    gross = _profit(row.symbol, row.signal.side, 0.01, entry_price, exit_price)
                    spread_cost = abs(_profit(row.symbol, "buy", 0.01, entry_price, entry_price + row.spread_price))
                    events_by_config[name].append({
                        "signal_time": row.dt,
                        "exit_time": row.rates.iloc[int(result["exit_idx"])]["time"].to_pydatetime(),
                        "channel": row.signal.chat_title,
                        "status": result["status"],
                        "pnl": gross - spread_cost,
                        "hold_minutes": (int(result["exit_idx"]) - int(result["entry_idx"])) * 5,
                    })

        split_time = start + (end - start) * 0.70
        results = []
        for name, events in events_by_config.items():
            results.append({
                "name": name,
                "full": _score(events),
                "train": _score([event for event in events if event["signal_time"] < split_time]),
                "holdout": _score([event for event in events if event["signal_time"] >= split_time]),
            })
        baseline = next(row for row in results if row["name"] == "baseline")
        for row in results:
            row["pnl_delta_vs_baseline"] = round(row["full"]["pnl_001"] - baseline["full"]["pnl_001"], 2)
            row["dd_delta_vs_baseline"] = round(row["full"]["max_dd_001"] - baseline["full"]["max_dd_001"], 2)
        robust = [row for row in results if row["train"]["pnl_001"] > 0 and row["holdout"]["pnl_001"] > 0]
        robust.sort(key=lambda row: (row["full"]["pnl_001"], row["full"]["max_dd_001"]), reverse=True)
        payload = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "approx_trading_sessions": 90,
            "timeframe": "M5",
            "signals": len(rows),
            "baseline": baseline,
            "best_robust": robust[0] if robust else None,
            "ranked_robust": robust,
            "all": sorted(results, key=lambda row: row["full"]["pnl_001"], reverse=True),
            "note": "Only legs targeting TP2+ are managed. Spread deducted; commissions excluded. Same-bar SL is evaluated before TP.",
        }
        out = ROOT / "data_vantage" / "live_trend_exit_90sessions_20260720.json"
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        print(json.dumps({"signals": len(rows), "baseline": baseline, "best_robust": payload["best_robust"], "top5": robust[:5]}, indent=2), flush=True)
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
