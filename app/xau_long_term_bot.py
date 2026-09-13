from __future__ import annotations

import csv
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.backtest_xau_long_term_10 import (
    STRATEGY_NAMES,
    _cot,
    _daily_candidates,
    _daily_features,
    _fred,
    _gld,
    _merge_available,
    _rates,
)

from .config import load_settings
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    close_position,
    connect,
    ensure_symbol,
    get_tick,
    modify_position,
    mt5,
    positions_by_magic,
    send_market_order,
    shutdown,
    symbol_info,
)
from .risk import normalize_volume


STATUS_FILE = "xau_long_term_status.json"
EVENTS_FILE = "xau_long_term_events.jsonl"
TRADES_FILE = "xau_long_term_trades.csv"
RUNTIME_FILE = "xau_long_term_runtime.json"

DEPLOYABLE_STRATEGIES = (
    "time_series_momentum",
    "breakout_retest",
    "volatility_squeeze",
    "macro_yield_usd",
    "cot_extreme",
    "gld_flow",
    "seasonality_trend",
    "gold_silver_ratio",
)
EXCLUDED_STRATEGIES = ("trend_pullback_h4", "donchian_breakout")
COMMENTS = {
    "time_series_momentum": "LT:TSM",
    "breakout_retest": "LT:RETEST",
    "volatility_squeeze": "LT:SQUEEZE",
    "macro_yield_usd": "LT:MACRO",
    "cot_extreme": "LT:COT",
    "gld_flow": "LT:GLD-FLOW",
    "seasonality_trend": "LT:SEASON",
    "gold_silver_ratio": "LT:GSR",
}


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "true" if default else "false") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def _append_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp_utc": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def _append_trade(path: Path, payload: dict[str, Any]) -> None:
    fields = [
        "closed_at_utc",
        "ticket",
        "strategy",
        "side",
        "volume",
        "entry",
        "exit",
        "profit",
        "signal_id",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({"closed_at_utc": datetime.now(UTC).isoformat(), **payload})


def _enabled_strategies() -> tuple[str, ...]:
    raw = os.getenv("XAU_LONG_TERM_STRATEGIES", ",".join(DEPLOYABLE_STRATEGIES))
    requested = tuple(dict.fromkeys(value.strip().lower() for value in raw.split(",") if value.strip()))
    unknown = sorted(set(requested) - set(STRATEGY_NAMES))
    if unknown:
        raise ValueError(f"Unknown XAU long-term strategies: {', '.join(unknown)}")
    blocked = sorted(set(requested) & set(EXCLUDED_STRATEGIES))
    if blocked:
        raise ValueError(f"Explicitly excluded strategies cannot be enabled: {', '.join(blocked)}")
    return requested


def _round_price(symbol: str, value: float) -> float:
    return round(float(value), int(getattr(symbol_info(symbol), "digits", 2) or 2))


def _signal_id(strategy: str, candidate: dict[str, Any]) -> str:
    signal_time = pd.Timestamp(candidate["signal_time"]).isoformat()
    return f"{strategy}|{signal_time}|{candidate['side']}"


def _refresh_source_cache(cache: Path, max_age_hours: float) -> None:
    cutoff = time.time() - max_age_hours * 3600.0
    for path in cache.glob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _build_candidates(root: Path, symbol: str, now: datetime) -> dict[str, list[dict[str, Any]]]:
    history_start = now - timedelta(days=900)
    d1 = _daily_features(_rates(symbol, mt5.TIMEFRAME_D1, history_start, now))
    silver_symbol = ensure_symbol("XAGUSD")
    silver = _rates(silver_symbol, mt5.TIMEFRAME_D1, history_start, now)
    cache = root / "data_vantage" / "research_sources"
    _refresh_source_cache(cache, _env_float("XAU_LONG_TERM_SOURCE_REFRESH_HOURS", 12.0))

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
    mean = d1["gsr"].rolling(252, min_periods=126).mean()
    std = d1["gsr"].rolling(252, min_periods=126).std(ddof=0)
    d1["gsr_z"] = (d1["gsr"] - mean) / std.replace(0.0, pd.NA)
    return _daily_candidates(
        d1,
        pd.Timestamp(now - timedelta(days=21)),
        pd.Timestamp(now),
    )


def _market_is_fresh(symbol: str, max_tick_age_seconds: float) -> bool:
    tick = get_tick(symbol)
    tick_epoch = float(getattr(tick, "time", 0.0) or 0.0)
    return (
        float(getattr(tick, "bid", 0.0) or 0.0) > 0
        and float(getattr(tick, "ask", 0.0) or 0.0) > 0
        and tick_epoch > 0
        and time.time() - tick_epoch <= max_tick_age_seconds
    )


def _newest_position(symbol: str, magic: int, before: set[int]):
    positions = [position for position in positions_by_magic(symbol, magic) if int(position.ticket) not in before]
    return max(
        positions,
        key=lambda position: int(getattr(position, "time_msc", 0) or getattr(position, "time", 0) or 0),
        default=None,
    )


def _open_candidate(
    symbol: str,
    strategy: str,
    candidate: dict[str, Any],
    signal_id: str,
    lot: float,
    magic: int,
    deviation: int,
    runtime: dict[str, Any],
    events_path: Path,
) -> bool:
    side = str(candidate["side"])
    tick = get_tick(symbol)
    entry = float(tick.ask if side == "buy" else tick.bid)
    risk = max(3.0, float(candidate["atr"]) * 0.45)
    sl = _round_price(symbol, entry - risk if side == "buy" else entry + risk)
    tp = _round_price(symbol, entry + 2.0 * risk if side == "buy" else entry - 2.0 * risk)
    info = symbol_info(symbol)
    volume = normalize_volume(
        symbol,
        lot,
        float(getattr(info, "volume_min", 0.01) or 0.01),
        float(getattr(info, "volume_max", lot) or lot),
    )
    before = {int(position.ticket) for position in positions_by_magic(symbol, magic)}
    result = send_market_order(symbol, side, volume, sl, tp, deviation, magic, COMMENTS[strategy])
    retcode = int(getattr(result, "retcode", -1) or -1)
    event = {
        "type": "order_attempt",
        "strategy": strategy,
        "signal_id": signal_id,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "volume": volume,
        "retcode": retcode,
        "broker_comment": str(getattr(result, "comment", "") or ""),
    }
    accepted = retcode in {10008, 10009, 10010}
    if accepted:
        position = _newest_position(symbol, magic, before)
        ticket = int(getattr(position, "ticket", 0) or getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
        runtime.setdefault("positions", {})[str(ticket)] = {
            "strategy": strategy,
            "signal_id": signal_id,
            "side": side,
            "entry": entry,
            "initial_risk": risk,
            "best_price": entry,
            "volume": volume,
            "opened_utc": datetime.now(UTC).isoformat(),
            "magic": magic,
        }
        event["ticket"] = ticket
    _append_event(events_path, event)
    return accepted


def _closed_profit(ticket: int, opened_utc: str) -> tuple[float, float]:
    deals = list(mt5.history_deals_get(position=ticket) or [])
    if not deals:
        try:
            opened = datetime.fromisoformat(opened_utc.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            opened = datetime.now() - timedelta(days=30)
        deals = [
            deal
            for deal in (mt5.history_deals_get(opened - timedelta(minutes=5), datetime.now() + timedelta(minutes=5)) or [])
            if int(getattr(deal, "position_id", 0) or 0) == ticket
        ]
    profit = sum(
        float(getattr(deal, "profit", 0.0) or 0.0)
        + float(getattr(deal, "commission", 0.0) or 0.0)
        + float(getattr(deal, "swap", 0.0) or 0.0)
        for deal in deals
    )
    exit_price = float(getattr(deals[-1], "price", 0.0) or 0.0) if deals else 0.0
    return profit, exit_price


def _manage_positions(
    symbol: str,
    magics: dict[str, int],
    runtime: dict[str, Any],
    events_path: Path,
    trades_path: Path,
    max_hold_days: float,
) -> None:
    current = {
        str(position.ticket): position
        for magic in magics.values()
        for position in positions_by_magic(symbol, magic)
    }
    metadata = runtime.setdefault("positions", {})
    for ticket, item in list(metadata.items()):
        position = current.get(ticket)
        if position is None:
            profit, exit_price = _closed_profit(int(ticket), str(item.get("opened_utc", "")))
            _append_trade(
                trades_path,
                {
                    "ticket": ticket,
                    "strategy": item.get("strategy", ""),
                    "side": item.get("side", ""),
                    "volume": item.get("volume", 0.0),
                    "entry": item.get("entry", 0.0),
                    "exit": exit_price,
                    "profit": round(profit, 2),
                    "signal_id": item.get("signal_id", ""),
                },
            )
            _append_event(events_path, {"type": "position_closed", "ticket": ticket, "strategy": item.get("strategy"), "profit": round(profit, 2)})
            metadata.pop(ticket, None)
            continue

        side = str(item.get("side", "buy"))
        entry = float(item.get("entry", position.price_open) or position.price_open)
        risk = max(0.01, float(item.get("initial_risk", abs(entry - float(position.sl or entry))) or 0.01))
        tick = get_tick(symbol)
        current_price = float(tick.bid if side == "buy" else tick.ask)
        best = float(item.get("best_price", entry) or entry)
        best = max(best, current_price) if side == "buy" else min(best, current_price)
        item["best_price"] = best
        favorable = best - entry if side == "buy" else entry - best
        current_sl = float(position.sl or 0.0)
        desired_sl = current_sl
        if favorable >= risk:
            desired_sl = max(desired_sl, entry) if side == "buy" else min(desired_sl or entry, entry)
        if favorable >= 1.5 * risk:
            trail = best - risk if side == "buy" else best + risk
            desired_sl = max(desired_sl, trail) if side == "buy" else min(desired_sl or trail, trail)
        if abs(desired_sl - current_sl) >= float(getattr(symbol_info(symbol), "point", 0.01) or 0.01):
            result = modify_position(position, _round_price(symbol, desired_sl), float(position.tp or 0.0))
            _append_event(
                events_path,
                {
                    "type": "stop_update",
                    "ticket": ticket,
                    "strategy": item.get("strategy"),
                    "old_sl": current_sl,
                    "new_sl": desired_sl,
                    "retcode": int(getattr(result, "retcode", -1) or -1),
                },
            )

        try:
            opened = datetime.fromisoformat(str(item.get("opened_utc", "")).replace("Z", "+00:00"))
        except Exception:
            opened = datetime.now(UTC)
        if datetime.now(UTC) - opened >= timedelta(days=max_hold_days):
            result = close_position(position, 50)
            _append_event(events_path, {"type": "time_exit", "ticket": ticket, "strategy": item.get("strategy"), "retcode": int(getattr(result, "retcode", -1) or -1)})


def run() -> None:
    cfg = load_settings()
    data_dir = Path(os.getenv("XAU_LONG_TERM_DATA_DIR", "data_vantage_long_term"))
    if not data_dir.is_absolute():
        data_dir = cfg.base_dir / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    status_path = data_dir / STATUS_FILE
    if not _env_bool("XAU_LONG_TERM_ENABLED", True):
        _write_json(
            status_path,
            {
                "updated_utc": datetime.now(UTC).isoformat(),
                "running": False,
                "enabled": False,
                "reason": "disabled_by_profile",
                "open_positions": 0,
            },
        )
        return
    strategies = _enabled_strategies()
    events_path = data_dir / EVENTS_FILE
    trades_path = data_dir / TRADES_FILE
    runtime_path = data_dir / RUNTIME_FILE
    runtime = _read_json(runtime_path, {"processed": [], "positions": {}, "attempts": {}})
    runtime.setdefault("processed", [])
    runtime.setdefault("positions", {})
    runtime.setdefault("attempts", {})

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    account = account_info()
    is_demo = "demo" in str(getattr(account, "server", "") or "").lower()
    if not is_demo and not _env_bool("XAU_LONG_TERM_ALLOW_LIVE_ACCOUNT", False):
        shutdown()
        raise RuntimeError("XAU long-term bot refuses a non-demo account without XAU_LONG_TERM_ALLOW_LIVE_ACCOUNT=true")
    symbol = ensure_symbol(os.getenv("SYMBOL", "XAUUSD"))
    lot = max(0.01, _env_float("XAU_LONG_TERM_LOT", 0.01))
    magic_base = int(_env_float("XAU_LONG_TERM_MAGIC_BASE", 998100))
    entry_magics = {strategy: magic_base + DEPLOYABLE_STRATEGIES.index(strategy) for strategy in strategies}
    management_magics = {
        strategy: magic_base + DEPLOYABLE_STRATEGIES.index(strategy)
        for strategy in DEPLOYABLE_STRATEGIES
    }
    loop_seconds = max(5.0, _env_float("XAU_LONG_TERM_LOOP_SECONDS", 60.0))
    refresh_seconds = max(60.0, _env_float("XAU_LONG_TERM_SIGNAL_REFRESH_SECONDS", 300.0))
    signal_max_age = max(3600.0, _env_float("XAU_LONG_TERM_SIGNAL_MAX_AGE_SECONDS", 259200.0))
    retry_seconds = max(30.0, _env_float("XAU_LONG_TERM_RETRY_SECONDS", 300.0))
    max_tick_age = max(30.0, _env_float("XAU_LONG_TERM_MAX_TICK_AGE_SECONDS", 300.0))
    max_hold_days = max(1.0, _env_float("XAU_LONG_TERM_MAX_HOLD_DAYS", 14.0))
    deviation = int(_env_float("XAU_LONG_TERM_DEVIATION", 50))
    last_refresh = 0.0
    latest_candidates = {strategy: [] for strategy in strategies}
    last_error = ""
    _append_event(events_path, {"type": "started", "account": int(account.login), "symbol": symbol, "strategies": list(strategies), "unlimited_positions": True})

    try:
        while True:
            now = datetime.now(UTC)
            try:
                # Keep managing positions opened by a strategy that was later disabled.
                _manage_positions(symbol, management_magics, runtime, events_path, trades_path, max_hold_days)
                if time.time() - last_refresh >= refresh_seconds:
                    all_candidates = _build_candidates(cfg.base_dir, symbol, now)
                    latest_candidates = {strategy: all_candidates.get(strategy, []) for strategy in strategies}
                    last_refresh = time.time()
                processed = set(str(value) for value in runtime.get("processed", []))
                attempts = runtime.setdefault("attempts", {})
                market_fresh = _market_is_fresh(symbol, max_tick_age)
                for strategy in strategies:
                    for candidate in latest_candidates.get(strategy, []):
                        signal_id = _signal_id(strategy, candidate)
                        if signal_id in processed:
                            continue
                        age = (pd.Timestamp(now) - pd.Timestamp(candidate["signal_time"])).total_seconds()
                        if age > signal_max_age:
                            processed.add(signal_id)
                            _append_event(events_path, {"type": "signal_expired", "strategy": strategy, "signal_id": signal_id, "age_seconds": round(age, 1)})
                            continue
                        if not market_fresh:
                            continue
                        if time.time() - float(attempts.get(signal_id, 0.0) or 0.0) < retry_seconds:
                            continue
                        attempts[signal_id] = time.time()
                        accepted = _open_candidate(
                            symbol,
                            strategy,
                            candidate,
                            signal_id,
                            lot,
                            entry_magics[strategy],
                            deviation,
                            runtime,
                            events_path,
                        )
                        if accepted:
                            processed.add(signal_id)
                runtime["processed"] = sorted(processed)[-5000:]
                runtime["attempts"] = {key: value for key, value in attempts.items() if key not in processed}
                _write_json(runtime_path, runtime)
                account = account_info()
                open_count = sum(len(positions_by_magic(symbol, magic)) for magic in management_magics.values())
                status = {
                    "updated_utc": now.isoformat(),
                    "running": True,
                    "last_error": "",
                    "account": int(account.login),
                    "server": str(account.server),
                    "balance": round(float(account.balance), 2),
                    "equity": round(float(account.equity), 2),
                    "symbol": symbol,
                    "lot_per_position": lot,
                    "unlimited_positions": True,
                    "open_positions": open_count,
                    "processed_signals": len(processed),
                    "enabled_strategies": [STRATEGY_NAMES[strategy] for strategy in strategies],
                    "excluded_strategies": [STRATEGY_NAMES[strategy] for strategy in EXCLUDED_STRATEGIES],
                    "latest_raw_signals": {strategy: len(rows) for strategy, rows in latest_candidates.items()},
                }
                _write_json(status_path, status)
                last_error = ""
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                _append_event(events_path, {"type": "loop_error", "error": last_error})
                _write_json(
                    status_path,
                    {
                        "updated_utc": now.isoformat(),
                        "running": True,
                        "last_error": last_error,
                        "enabled_strategies": [STRATEGY_NAMES[strategy] for strategy in strategies],
                        "unlimited_positions": True,
                    },
                )
            time.sleep(loop_seconds)
    finally:
        if last_error:
            _append_event(events_path, {"type": "stopped", "error": last_error})
        shutdown()
