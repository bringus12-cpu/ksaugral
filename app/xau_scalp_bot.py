from __future__ import annotations

import csv
import json
import os
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import load_settings
from .indicators import enrich
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    calc_loss_per_lot,
    close_position,
    connect,
    ensure_symbol,
    get_rates_df,
    get_tick,
    modify_position,
    mt5,
    positions_by_magic,
    send_market_order,
    shutdown,
    trading_status,
)
from .risk import current_spread_points, normalize_volume, trading_day_key
from .scalp_setups import EXTRA_SETUP_MODES, INDICATOR_SETUP_PROFILES, evaluate_extra_setup


STATUS_FILE = "xau_scalp_status.json"
EVENTS_FILE = "xau_scalp_events.jsonl"
TRADES_FILE = "xau_scalp_trades.csv"
META_FILE = "xau_scalp_meta.json"
RUNTIME_FILE = "xau_scalp_runtime.json"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict) -> None:
    event = {"timestamp_utc": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def _ensure_trades(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["closed_at_utc", "ticket", "side", "volume", "entry", "exit", "profit", "reason", "strategy"],
        )
        writer.writeheader()


def _append_trade(path: Path, payload: dict) -> None:
    _ensure_trades(path)
    row = {"closed_at_utc": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writerow(row)


def _closed_position_summary(ticket: str, item: dict) -> tuple[float, float, float, float]:
    position_id = int(ticket)
    deals = list(mt5.history_deals_get(position=position_id) or [])
    if not deals:
        try:
            created = datetime.fromisoformat(str(item.get("created_utc", ""))).replace(tzinfo=None)
        except Exception:
            created = datetime.now() - timedelta(days=7)
        deals = [
            deal
            for deal in (mt5.history_deals_get(created - timedelta(minutes=5), datetime.now() + timedelta(minutes=5)) or [])
            if int(getattr(deal, "position_id", 0) or 0) == position_id
        ]
    gross = sum(float(getattr(deal, "profit", 0.0) or 0.0) for deal in deals)
    commission = sum(float(getattr(deal, "commission", 0.0) or 0.0) for deal in deals)
    swap = sum(float(getattr(deal, "swap", 0.0) or 0.0) for deal in deals)
    exit_price = float(item.get("last_price", item.get("entry", 0.0)) or 0.0)
    closing_deals = [deal for deal in deals if int(getattr(deal, "entry", -1) or -1) != 0]
    if closing_deals:
        exit_price = float(getattr(closing_deals[-1], "price", exit_price) or exit_price)
    elif deals:
        exit_price = float(getattr(deals[-1], "price", exit_price) or exit_price)
    return gross + commission + swap, gross, commission + swap, exit_price


def _side_price(symbol: str, side: str) -> float:
    tick = get_tick(symbol)
    return float(tick.ask if side == "buy" else tick.bid)


def _sync_role() -> str:
    return str(os.environ.get("XAU_SCALP_SYNC_ROLE", "") or "").strip().lower()


def _sync_ttl_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("XAU_SCALP_SYNC_TTL_SECONDS", "30") or 30.0))
    except Exception:
        return 30.0


def _sync_file(cfg) -> Path:
    raw = str(os.environ.get("XAU_SCALP_SYNC_FILE", "") or "").strip()
    if not raw:
        raw = "data/xau_scalp_shared_signal.json"
    path = Path(raw)
    if not path.is_absolute():
        path = cfg.base_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _publish_sync_signal(path: Path, signal: dict, cfg, symbol: str, diagnostics: dict) -> None:
    payload = {
        "id": f"{datetime.now(UTC).timestamp():.3f}:{signal.get('side')}:{signal.get('entry')}",
        "created_utc": datetime.now(UTC).isoformat(),
        "source": "xau_scalp_master",
        "account": int(getattr(account_info(), "login", 0) or 0),
        "symbol": symbol,
        "signal": signal,
        "diagnostics": diagnostics,
        "ttl_seconds": _sync_ttl_seconds(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _read_sync_signal(path: Path, symbol: str, runtime: dict) -> tuple[dict | None, dict]:
    if not path.exists():
        return None, {"reason": "sync_waiting_for_master"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, {"reason": "sync_read_error", "error": type(exc).__name__}
    signal_id = str(payload.get("id", "") or "")
    if not signal_id:
        return None, {"reason": "sync_signal_missing_id"}
    if str(runtime.get("last_sync_signal_id", "") or "") == signal_id:
        return None, {"reason": "sync_signal_already_used", "sync_id": signal_id}
    try:
        created = datetime.fromisoformat(str(payload.get("created_utc", "")).replace("Z", "+00:00"))
    except Exception:
        return None, {"reason": "sync_signal_bad_time", "sync_id": signal_id}
    age = (datetime.now(UTC) - created).total_seconds()
    ttl = float(payload.get("ttl_seconds", _sync_ttl_seconds()) or _sync_ttl_seconds())
    if age > ttl:
        return None, {"reason": "sync_signal_expired", "sync_id": signal_id, "age_seconds": round(age, 2)}

    source_signal = dict(payload.get("signal") or {})
    side = str(source_signal.get("side", "") or "").lower()
    if side not in {"buy", "sell"}:
        return None, {"reason": "sync_signal_bad_side", "sync_id": signal_id}
    source_entry = float(source_signal.get("entry", 0.0) or 0.0)
    source_sl = float(source_signal.get("sl", 0.0) or 0.0)
    source_tp1 = float(source_signal.get("tp1", 0.0) or 0.0)
    source_tp2 = float(source_signal.get("tp2", 0.0) or 0.0)
    if min(source_entry, source_sl, source_tp1, source_tp2) <= 0:
        return None, {"reason": "sync_signal_bad_levels", "sync_id": signal_id}

    entry = _side_price(symbol, side)
    if side == "buy":
        sl_dist = max(0.1, source_entry - source_sl)
        tp1_dist = max(0.1, source_tp1 - source_entry)
        tp2_dist = max(tp1_dist, source_tp2 - source_entry)
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
        tp2 = entry + tp2_dist
    else:
        sl_dist = max(0.1, source_sl - source_entry)
        tp1_dist = max(0.1, source_entry - source_tp1)
        tp2_dist = max(tp1_dist, source_entry - source_tp2)
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
        tp2 = entry - tp2_dist
    signal = {
        "strategy": "xau scalp sync",
        "side": side,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "source_entry": round(source_entry, 2),
        "sync_id": signal_id,
        "reason": "sync_from_master",
    }
    return signal, {"reason": "sync_signal_ready", "sync_id": signal_id, "age_seconds": round(age, 2), "source_symbol": payload.get("symbol")}


def _status_position(position) -> dict:
    price_open = float(getattr(position, "price_open", 0.0) or 0.0)
    comment = str(getattr(position, "comment", "") or "")
    return {
        "ticket": int(getattr(position, "ticket", 0) or 0),
        "symbol": str(getattr(position, "symbol", "")),
        "side": "BUY" if int(getattr(position, "type", 0) or 0) == 0 else "SELL",
        "leg": "tp2" if "tp2" in comment else "tp1",
        "volume": float(getattr(position, "volume", 0.0) or 0.0),
        "open": price_open,
        "price_open": price_open,
        "current": float(getattr(position, "price_current", 0.0) or 0.0),
        "sl": float(getattr(position, "sl", 0.0) or 0.0),
        "tp": float(getattr(position, "tp", 0.0) or 0.0),
        "profit": float(getattr(position, "profit", 0.0) or 0.0),
        "comment": comment,
        "time": int(getattr(position, "time", 0) or 0),
    }


def _daily_closed_profit(path: Path) -> float:
    today = datetime.now(UTC).date().isoformat()
    if not path.exists():
        return 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = csv.DictReader(handle)
            return sum(float(row.get("profit", 0.0) or 0.0) for row in rows if str(row.get("closed_at_utc", "")).startswith(today))
    except Exception:
        return 0.0


def _daily_side_stats(path: Path) -> dict:
    stats = {
        "buy": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "win_rate": 0.0},
        "sell": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "win_rate": 0.0},
    }
    today = datetime.now(UTC).date().isoformat()
    if not path.exists():
        return stats
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if not str(row.get("closed_at_utc", "")).startswith(today):
                    continue
                side = str(row.get("side", "") or "").strip().lower()
                if side not in stats:
                    continue
                profit = float(row.get("profit", 0.0) or 0.0)
                stats[side]["trades"] += 1
                stats[side]["profit"] += profit
                if profit > 0:
                    stats[side]["wins"] += 1
                elif profit < 0:
                    stats[side]["losses"] += 1
    except Exception:
        return stats
    for side, row in stats.items():
        decisive = int(row["wins"]) + int(row["losses"])
        row["profit"] = round(float(row["profit"]), 2)
        row["win_rate"] = round(float(row["wins"]) / max(1, decisive) * 100.0, 2)
    return stats


def _session_filter_status(now: datetime) -> dict:
    enabled = _env_bool("XAU_SCALP_SESSION_FILTER_ENABLED", False)
    raw = str(os.getenv("XAU_SCALP_ACTIVE_UTC_SESSIONS", "06:00-16:30") or "").strip()
    status = {"enabled": enabled, "active": True, "sessions_utc": raw}
    if not enabled or not raw:
        return status
    minutes_now = (now.hour * 60) + now.minute
    active = False
    for chunk in raw.split(","):
        if "-" not in chunk:
            continue
        start_raw, end_raw = [part.strip() for part in chunk.split("-", 1)]
        try:
            start_h, start_m = [int(part) for part in start_raw.split(":", 1)]
            end_h, end_m = [int(part) for part in end_raw.split(":", 1)]
        except Exception:
            continue
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end:
            active = active or (start <= minutes_now <= end)
        else:
            active = active or (minutes_now >= start or minutes_now <= end)
    status["active"] = active
    return status


def _spread_quality_ok(spread_points: float, signal: dict | None, cfg) -> tuple[bool, dict]:
    max_points = float(getattr(cfg, "xau_scalp_max_spread_points", 0.0) or 0.0)
    max_tp1_pct = _env_float("XAU_SCALP_MAX_SPREAD_TP1_PCT", 0.0)
    status = {
        "spread_points": spread_points,
        "max_spread_points": max_points,
        "max_spread_tp1_pct": max_tp1_pct,
        "ok": spread_points <= max_points if max_points > 0 else True,
    }
    if signal and max_tp1_pct > 0:
        tp1_dist = abs(float(signal.get("tp1", 0.0) or 0.0) - float(signal.get("entry", 0.0) or 0.0))
        point = _env_float("XAU_SCALP_SPREAD_POINT_VALUE_USD", 0.01)
        spread_usd = float(spread_points) * max(0.00001, point)
        status["tp1_distance_usd"] = round(tp1_dist, 4)
        status["spread_usd_estimate"] = round(spread_usd, 4)
        if tp1_dist > 0 and spread_usd > tp1_dist * max_tp1_pct:
            status["ok"] = False
            status["reason"] = "spread_too_large_vs_tp1"
    elif spread_points > max_points:
        status["reason"] = "spread_too_wide"
    return bool(status["ok"]), status


def _side_quality_block_remaining(path: Path, side: str) -> tuple[float, dict]:
    if not _env_bool("XAU_SCALP_SIDE_QUALITY_BLOCK_ENABLED", True):
        return 0.0, {"enabled": False}
    stats = _daily_side_stats(path)
    row = stats.get(side, {})
    min_trades = int(_env_float("XAU_SCALP_SIDE_QUALITY_MIN_TRADES", 4))
    min_win_rate = _env_float("XAU_SCALP_SIDE_QUALITY_MIN_WIN_RATE", 35.0)
    block_seconds = max(60.0, _env_float("XAU_SCALP_SIDE_QUALITY_BLOCK_SECONDS", 1800.0))
    status = {"enabled": True, "side": side, "stats": row, "min_trades": min_trades, "min_win_rate": min_win_rate}
    if int(row.get("trades", 0) or 0) < min_trades or float(row.get("win_rate", 0.0) or 0.0) >= min_win_rate:
        return 0.0, status
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [r for r in csv.DictReader(handle) if str(r.get("side", "")).strip().lower() == side]
        last_closed = datetime.fromisoformat(str(rows[-1].get("closed_at_utc", "")).replace("Z", "+00:00"))
    except Exception:
        return block_seconds, {**status, "blocked": True, "remaining_seconds": block_seconds}
    remaining = max(0.0, block_seconds - (datetime.now(UTC) - last_closed).total_seconds())
    return remaining, {**status, "blocked": remaining > 0, "remaining_seconds": round(remaining, 1)}


def _recent_full_loss_batches(path: Path, *, batch_size: int = 3, batches: int = 2) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return False
    needed = int(batch_size) * int(batches)
    if len(rows) < needed:
        return False
    recent = rows[-needed:]
    try:
        profits = [float(row.get("profit", 0.0) or 0.0) for row in recent]
    except Exception:
        return False
    return all(value < 0.0 for value in profits)


def _recent_full_loss_batch(path: Path, *, batch_size: int = 3) -> bool:
    return _recent_full_loss_batches(path, batch_size=batch_size, batches=1)


def _loss_pause_enabled() -> bool:
    raw = str(os.getenv("XAU_SCALP_LOSS_PAUSE_ENABLED", "true") or "true").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _opposite_impulse_filter_enabled() -> bool:
    raw = str(os.getenv("XAU_SCALP_OPPOSITE_IMPULSE_FILTER_ENABLED", "true") or "true").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _same_direction_chase_filter_enabled() -> bool:
    raw = str(os.getenv("XAU_SCALP_SAME_DIRECTION_CHASE_FILTER_ENABLED", "true") or "true").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "true" if default else "false") or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _scaled_initial_sl_distance(base_distance: float) -> tuple[float, float]:
    multiplier = min(3.0, max(0.25, _env_float("XAU_SCALP_SL_DISTANCE_MULTIPLIER", 1.0)))
    return max(0.1, float(base_distance) * multiplier), multiplier


def _scaled_tp_distance(base_distance: float) -> tuple[float, float]:
    multiplier = min(2.0, max(0.25, _env_float("XAU_SCALP_TP_DISTANCE_MULTIPLIER", 1.0)))
    return max(0.25, float(base_distance) * multiplier), multiplier


def _strategy_name() -> str:
    raw = str(os.getenv("XAU_SCALP_STRATEGY_NAME", "XAU-SCALP") or "XAU-SCALP").strip()
    return raw[:16]


def _strategy_comment(leg: str, signal: dict | None = None) -> str:
    setup_tag = str((signal or {}).get("setup_tag", "") or _strategy_name()).strip()
    return f"{setup_tag}:{leg}"[:31]


def _min_hold_seconds() -> float:
    base = max(0.0, _env_float("UPCOMERS_MIN_HOLD_SECONDS", 0.0))
    jitter = max(0.0, _env_float("UPCOMERS_MIN_HOLD_JITTER_SECONDS", 0.0))
    if base <= 0:
        return 0.0
    return max(1.0, base + random.uniform(-jitter, jitter))


def _delay_broker_levels_for_min_hold() -> bool:
    return _min_hold_seconds() > 0 and _env_bool("UPCOMERS_DELAY_BROKER_TP_SL", True)


def _position_age_seconds(position) -> float:
    opened = int(getattr(position, "time", 0) or 0)
    if opened <= 0:
        return 999999.0
    return max(0.0, time.time() - float(opened))


def _hold_remaining_seconds(position, item: dict) -> float:
    target = float(item.get("min_hold_seconds", 0.0) or 0.0)
    if target <= 0:
        return 0.0
    created_raw = str(item.get("created_utc", "") or "")
    if created_raw:
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = max(0.0, (datetime.now(UTC) - created.astimezone(UTC)).total_seconds())
            return max(0.0, target - age)
        except Exception:
            pass
    return max(0.0, target - _position_age_seconds(position))


def _xau_scalp_total_lot(symbol: str, cfg, balance: float, runtime: dict) -> tuple[float, int]:
    if not bool(getattr(cfg, "xau_scalp_dynamic_lot_enabled", False)):
        return normalize_volume(symbol, float(cfg.xau_scalp_lot), float(cfg.min_lot), float(cfg.max_lot)), 0
    step = max(1.0, float(getattr(cfg, "xau_scalp_dynamic_step_usd", 1000.0) or 1000.0))
    add = max(0.0, float(getattr(cfg, "xau_scalp_dynamic_lot_add", 0.01) or 0.01))
    basis = str(os.getenv("XAU_SCALP_DYNAMIC_LOT_BASIS", "profit") or "profit").strip().lower()
    reference_id = str(os.getenv("XAU_SCALP_DYNAMIC_REFERENCE_ID", "default") or "default").strip()
    configured_reference = max(0.0, _env_float("XAU_SCALP_DYNAMIC_BASE_BALANCE_USD", 0.0))
    if configured_reference > 0.0:
        runtime["dynamic_reference_id"] = reference_id
        runtime["dynamic_reference_balance"] = configured_reference
    elif str(runtime.get("dynamic_reference_id", "") or "") != reference_id:
        runtime["dynamic_reference_id"] = reference_id
        runtime["dynamic_reference_balance"] = float(balance or 0.0)
    reference = float(runtime.setdefault("dynamic_reference_balance", float(balance or 0.0)) or balance or 0.0)
    if basis == "balance_per_leg":
        steps = max(1, int(max(0.0, float(balance or 0.0)) // step))
        leg_lot = max(float(cfg.min_lot), steps * add)
        raw_lot = leg_lot * _xau_scalp_leg_count()
    elif basis == "balance":
        steps = max(1, int(max(0.0, float(balance or 0.0)) // step))
        raw_lot = max(float(cfg.xau_scalp_lot), steps * add)
    else:
        dynamic_profit = max(0.0, float(balance or 0.0) - reference)
        steps = int(dynamic_profit // step)
        raw_lot = float(cfg.xau_scalp_lot) + (steps * add)
    max_lot = max(float(cfg.min_lot), float(getattr(cfg, "xau_scalp_dynamic_max_lot", cfg.max_lot) or cfg.max_lot))
    return normalize_volume(symbol, min(raw_lot, max_lot), float(cfg.min_lot), max_lot), steps


def _xau_scalp_risk_leg_lot(symbol: str, signal: dict | None, balance: float, cfg, leg_count: int) -> float | None:
    risk_pct = max(0.0, _env_float("XAU_SCALP_RISK_PCT", 0.0))
    per_leg_risk_pct = max(0.0, _env_float("XAU_SCALP_RISK_PCT_PER_LEG", 0.0))
    if risk_pct <= 0.0 and per_leg_risk_pct <= 0.0:
        return None
    if signal:
        entry = float(signal.get("entry", 0.0) or 0.0)
        sl = float(signal.get("sl", 0.0) or 0.0)
        side = str(signal.get("side", "") or "").lower()
    else:
        tick = get_tick(symbol)
        entry = float(tick.ask)
        sl = entry - float(cfg.xau_scalp_sl_usd)
        side = "buy"
    if entry <= 0.0 or sl <= 0.0 or side not in {"buy", "sell"}:
        return None
    loss_per_lot = calc_loss_per_lot(symbol, side, entry, sl)
    if loss_per_lot <= 0.0:
        return None
    if per_leg_risk_pct > 0.0:
        risk_per_leg = float(balance) * (per_leg_risk_pct / 100.0)
    else:
        risk_per_leg = float(balance) * (risk_pct / 100.0) / max(1, int(leg_count))
    minimum_leg_lot = max(float(cfg.min_lot), _env_float("XAU_SCALP_MIN_LEG_LOT", float(cfg.min_lot)))
    return normalize_volume(
        symbol,
        max(minimum_leg_lot, risk_per_leg / loss_per_lot),
        float(cfg.min_lot),
        float(cfg.max_lot),
    )


def _account_risk_base(account: object) -> float:
    balance = float(getattr(account, "balance", 0.0) or 0.0)
    equity = float(getattr(account, "equity", 0.0) or 0.0)
    basis = str(os.getenv("POSITION_RISK_BASE", "lower") or "lower").strip().lower()
    if basis == "equity":
        return max(0.0, equity or balance)
    if basis == "balance":
        return max(0.0, balance or equity)
    if balance <= 0.0:
        return max(0.0, equity)
    if equity <= 0.0:
        return balance
    return min(balance, equity)


def _xau_scalp_leg_count() -> int:
    raw = os.getenv("XAU_SCALP_LEG_COUNT", "3").strip()
    try:
        return min(8, max(1, int(raw)))
    except ValueError:
        return 3


def _xau_scalp_target_plan() -> list[str]:
    """Return the configured TP distribution, keeping the current layout by default."""
    count = _xau_scalp_leg_count()
    raw = os.getenv("XAU_SCALP_TARGET_PLAN", "tp1,tp2,tp2")
    items = [part.strip().lower() for part in raw.split(",") if part.strip().lower() in {"tp1", "tp2"}]
    if not items:
        items = ["tp1", "tp2", "tp2"]
    while len(items) < count:
        items.append(items[-1])
    return items[:count]


def _xau_scalp_target_r_plan() -> list[float]:
    """Return optional per-leg reward targets expressed as multiples of initial risk."""
    raw = str(os.getenv("XAU_SCALP_TARGET_R_PLAN", "") or "").strip()
    if not raw:
        return []
    values: list[float] = []
    for part in raw.split(","):
        try:
            value = float(part.strip().lower().removesuffix("r"))
        except ValueError:
            continue
        if value > 0.0:
            values.append(min(12.0, value))
    count = _xau_scalp_leg_count()
    if not values:
        return []
    while len(values) < count:
        values.append(values[-1])
    return values[:count]


def _xau_scalp_legs(signal: dict) -> list[tuple[str, float]]:
    r_plan = _xau_scalp_target_r_plan()
    if r_plan:
        entry = float(signal["entry"])
        sl_distance = abs(entry - float(signal["sl"]))
        direction = 1.0 if str(signal["side"]).lower() == "buy" else -1.0
        return [
            (f"r{index + 1}_{target_r:g}", entry + (direction * sl_distance * target_r))
            for index, target_r in enumerate(r_plan)
        ]
    counts: dict[str, int] = {"tp1": 0, "tp2": 0}
    legs: list[tuple[str, float]] = []
    for target in _xau_scalp_target_plan():
        counts[target] += 1
        suffix = "" if counts[target] == 1 else chr(ord("a") + counts[target] - 2)
        legs.append((f"{target}{suffix}", signal[target]))
    return legs


def _backfill_position_meta(position, cfg, item: dict) -> bool:
    changed = False
    side = "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell"
    entry = float(getattr(position, "price_open", 0.0) or 0.0)
    comment = str(getattr(position, "comment", "") or "")
    leg = str(item.get("leg", "") or "") or ("tp2" if "tp2" in comment else "tp1")
    tp1_dist = max(0.1, float(cfg.xau_scalp_tp1_usd))
    tp2_dist = max(tp1_dist, float(cfg.xau_scalp_tp2_usd))
    sl_dist = max(0.1, float(cfg.xau_scalp_sl_usd))
    if side == "buy":
        fallback_sl = entry - sl_dist
        fallback_tp1 = entry + tp1_dist
        fallback_tp2 = entry + tp2_dist
    else:
        fallback_sl = entry + sl_dist
        fallback_tp1 = entry - tp1_dist
        fallback_tp2 = entry - tp2_dist
    defaults = {
        "created_utc": datetime.now(UTC).isoformat(),
        "min_hold_seconds": _min_hold_seconds(),
        "strategy": _strategy_name(),
        "leg": leg,
        "side": side,
        "volume": float(getattr(position, "volume", 0.0) or 0.0),
        "entry": entry,
        "tp1": round(fallback_tp1, 2),
        "tp2": round(fallback_tp2, 2),
        "sl": round(fallback_sl, 2),
        "be_done": False,
        "be_enabled": float(getattr(cfg, "xau_scalp_be_trigger_usd", 0.0) or 0.0) > 0.0,
        "be_trigger_usd": float(getattr(cfg, "xau_scalp_be_trigger_usd", 0.0) or 0.0),
        "be_buffer_usd": float(cfg.xau_scalp_be_buffer_usd),
        "max_hold_minutes": 0.0,
    }
    for key, value in defaults.items():
        current = item.get(key)
        if key == "min_hold_seconds":
            missing = current is None or current == ""
        elif key in {"tp1", "tp2", "sl", "entry", "volume"}:
            try:
                missing = float(current or 0.0) <= 0.0
            except Exception:
                missing = True
        else:
            missing = current in {None, ""}
        if missing:
            item[key] = value
            changed = True
    return changed


def _scalp_trailing_enabled() -> bool:
    return _env_bool("XAU_SCALP_TRAIL_ENABLED", True)


def _profit_lock_settings() -> dict[str, float | bool]:
    return {
        "enabled": _env_bool("XAU_SCALP_DAILY_PROFIT_LOCK_ENABLED", True),
        "trigger_usd": max(0.0, _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_USD", 0.0)),
        "trigger_balance_pct": max(0.0, _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_BALANCE_PCT", 0.0)),
        "giveback_usd": max(0.0, _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_USD", 0.0)),
        "giveback_pct": max(0.0, _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_PCT", 0.0)),
        "giveback_balance_pct": max(0.0, _env_float("XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_BALANCE_PCT", 0.0)),
        "hard_stop_usd": max(0.0, _env_float("XAU_SCALP_DAILY_PROFIT_STOP_USD", 0.0)),
    }


def _update_daily_profit_lock(
    runtime: dict,
    day_key: str,
    day_profit: float,
    day_start_balance: float,
    events_path: Path,
) -> tuple[bool, dict]:
    settings = _profit_lock_settings()
    if not bool(settings["enabled"]):
        return False, {"enabled": False}

    if runtime.get("profit_lock_day_key") != day_key:
        runtime["profit_lock_day_key"] = day_key
        runtime["daily_profit_peak_usd"] = round(day_profit, 2)
        runtime["daily_profit_locked"] = False
        runtime.pop("daily_profit_locked_reason", None)

    peak = max(float(runtime.get("daily_profit_peak_usd", day_profit) or day_profit), float(day_profit))
    runtime["daily_profit_peak_usd"] = round(peak, 2)

    locked = bool(runtime.get("daily_profit_locked", False))
    reason = str(runtime.get("daily_profit_locked_reason", "") or "")
    hard_stop = float(settings["hard_stop_usd"])
    trigger = max(
        float(settings["trigger_usd"]),
        float(day_start_balance) * float(settings["trigger_balance_pct"]) / 100.0,
    )
    giveback_usd = float(settings["giveback_usd"])
    giveback_pct = float(settings["giveback_pct"])
    giveback_balance = float(day_start_balance) * float(settings["giveback_balance_pct"]) / 100.0

    if not locked and hard_stop > 0.0 and day_profit >= hard_stop:
        locked = True
        reason = "daily_profit_target_hit"
    if not locked and trigger > 0.0 and peak >= trigger:
        allowed_giveback = max(giveback_usd, peak * giveback_pct, giveback_balance)
        if allowed_giveback > 0.0 and (peak - float(day_profit)) >= allowed_giveback:
            locked = True
            reason = "daily_profit_giveback_lock"

    if locked and not bool(runtime.get("daily_profit_locked", False)):
        runtime["daily_profit_locked"] = True
        runtime["daily_profit_locked_reason"] = reason
        _append_jsonl(
            events_path,
            {
                "type": "daily_profit_lock_started",
                "reason": reason,
                "day_profit": round(float(day_profit), 2),
                "peak_profit": round(peak, 2),
                "day_start_balance": round(float(day_start_balance), 2),
                "settings": settings,
            },
        )

    return bool(runtime.get("daily_profit_locked", locked)), {
        "enabled": True,
        "locked": bool(runtime.get("daily_profit_locked", locked)),
        "reason": reason,
        "day_profit": round(float(day_profit), 2),
        "peak_profit": round(peak, 2),
        "trigger_usd": trigger,
        "giveback_usd": max(giveback_usd, peak * giveback_pct, giveback_balance),
        "giveback_pct": giveback_pct,
        "trigger_balance_pct": float(settings["trigger_balance_pct"]),
        "giveback_balance_pct": float(settings["giveback_balance_pct"]),
        "day_start_balance": round(float(day_start_balance), 2),
        "hard_stop_usd": hard_stop,
    }


def _trailing_stop_candidate(side: str, entry: float, current: float, cfg) -> float | None:
    if not _scalp_trailing_enabled():
        return None
    start = max(
        float(getattr(cfg, "xau_scalp_be_trigger_usd", 0.0) or 0.0),
        _env_float("XAU_SCALP_TRAIL_START_USD", 3.0),
    )
    step = max(0.25, _env_float("XAU_SCALP_TRAIL_STEP_USD", 1.0))
    buffer = max(float(getattr(cfg, "xau_scalp_be_buffer_usd", 0.0) or 0.0), _env_float("XAU_SCALP_TRAIL_BE_BUFFER_USD", 0.15))
    move = current - entry if side == "buy" else entry - current
    if move < start:
        return None
    locked = buffer + (int((move - start) // step) * step)
    return entry + locked if side == "buy" else entry - locked


def _direction_loss_block_remaining(path: Path, side: str, *, batch_size: int = 3) -> float:
    enabled_raw = str(os.getenv("XAU_SCALP_DIRECTION_LOSS_BLOCK_ENABLED", "true") or "true").strip().lower()
    if enabled_raw not in {"1", "true", "yes", "y", "on"}:
        return 0.0
    if not path.exists() or side not in {"buy", "sell"}:
        return 0.0
    try:
        block_seconds = max(60.0, float(os.getenv("XAU_SCALP_DIRECTION_LOSS_BLOCK_SECONDS", "1800") or 1800.0))
    except Exception:
        block_seconds = 1800.0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return 0.0
    side_rows = [row for row in rows if str(row.get("side", "")).strip().lower() == side]
    needed = batch_size * 2
    if len(side_rows) < needed:
        return 0.0
    recent = side_rows[-needed:]
    try:
        profits = [float(row.get("profit", 0.0) or 0.0) for row in recent]
    except Exception:
        return 0.0
    if not all(value < 0.0 for value in profits):
        return 0.0
    try:
        last_closed = datetime.fromisoformat(str(recent[-1].get("closed_at_utc", "")).replace("Z", "+00:00"))
    except Exception:
        return 0.0
    elapsed = (datetime.now(UTC) - last_closed).total_seconds()
    return max(0.0, block_seconds - elapsed)


def _signal_from_market(symbol: str, cfg) -> tuple[dict | None, dict]:
    m1 = enrich(get_rates_df(symbol, "M1", 240))
    m5 = enrich(get_rates_df(symbol, "M5", 240))
    m15 = enrich(get_rates_df(symbol, "M15", 240))
    if len(m1) < 80 or len(m5) < 80 or len(m15) < 80:
        return None, {"reason": "rates_not_ready"}

    closed_only = _env_bool("XAU_SCALP_CLOSED_CANDLE_ONLY", False)
    current_idx = -2 if closed_only else -1
    previous_idx = -3 if closed_only else -2
    last1 = m1.iloc[current_idx]
    prev1 = m1.iloc[previous_idx]
    prev2 = m1.iloc[previous_idx - 1]
    prev3 = m1.iloc[previous_idx - 2]
    last5 = m5.iloc[current_idx]
    prev5 = m5.iloc[previous_idx]
    last15 = m15.iloc[current_idx]
    atr1 = float(last1.get("atr14", 0.0) or 0.0)
    atr5 = float(last5.get("atr14", atr1) or atr1)
    if atr1 <= 0 or atr5 <= 0:
        return None, {"reason": "atr_not_ready"}

    min_m5_adx = _env_float("XAU_SCALP_MIN_M5_ADX", 14.0)
    trend_buy = (
        float(last15["ema20"]) > float(last15["ema50"])
        and float(last5["ema20"]) > float(last5["ema50"])
        and float(last5["close"]) > float(last5["ema20"])
        and float(last5["adx14"]) >= min_m5_adx
    )
    trend_sell = (
        float(last15["ema20"]) < float(last15["ema50"])
        and float(last5["ema20"]) < float(last5["ema50"])
        and float(last5["close"]) < float(last5["ema20"])
        and float(last5["adx14"]) >= min_m5_adx
    )

    close1 = float(last1["close"])
    ema1 = float(last1["ema20"])
    prev_close = float(prev1["close"])
    prev_ema = float(prev1["ema20"])
    body = abs(float(last1["close"]) - float(last1["open"]))
    recent_m1 = m1.iloc[-5:-1] if closed_only else m1.tail(4)
    recent_move = close1 - float(recent_m1.iloc[0]["open"])
    m5_body = float(last5["close"]) - float(last5["open"])
    m5_progress = float(last5["close"]) - float(prev5["close"])
    max_chase = max(1.2, float(cfg.xau_scalp_tp1_usd) * 0.75)
    near_ema = abs(close1 - ema1) <= max_chase
    diagnostics = {
        "strategy": _strategy_name(),
        "closed_candle_only": closed_only,
        "signal_bar_utc": pd.Timestamp(last1["time"]).isoformat(),
        "m1_close": close1,
        "m1_ema20": ema1,
        "m5_adx14": float(last5["adx14"]),
        "m15_ema20": float(last15["ema20"]),
        "m15_ema50": float(last15["ema50"]),
        "near_ema": near_ema,
        "body": body,
        "m1_atr14": atr1,
        "m5_atr14": atr5,
        "recent_m1_move": recent_move,
        "m5_body": m5_body,
        "m5_progress": m5_progress,
    }

    opposite_filter_enabled = _opposite_impulse_filter_enabled()
    opposite_impulse_buy = opposite_filter_enabled and (
        recent_move < -(atr1 * 1.0) or m5_body < -(atr1 * 0.65) or m5_progress < -(atr1 * 0.75)
    )
    opposite_impulse_sell = opposite_filter_enabled and (
        recent_move > (atr1 * 1.0) or m5_body > (atr1 * 0.65) or m5_progress > (atr1 * 0.75)
    )
    chase_filter_enabled = _same_direction_chase_filter_enabled()
    max_m1_chase_usd = max(0.5, _env_float("XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD", 2.4))
    max_m5_chase_usd = max(0.5, _env_float("XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD", 3.5))
    buy_chasing_impulse = chase_filter_enabled and (recent_move > max_m1_chase_usd or m5_progress > max_m5_chase_usd)
    sell_chasing_impulse = chase_filter_enabled and (recent_move < -max_m1_chase_usd or m5_progress < -max_m5_chase_usd)
    diagnostics.update(
        {
            "same_direction_chase_filter": chase_filter_enabled,
            "max_m1_chase_usd": max_m1_chase_usd,
            "max_m5_chase_usd": max_m5_chase_usd,
            "buy_chasing_impulse": buy_chasing_impulse,
            "sell_chasing_impulse": sell_chasing_impulse,
        }
    )

    trigger_mode = str(os.getenv("XAU_SCALP_TRIGGER_MODE", "trend_pullback") or "trend_pullback").strip().lower()
    buy_trigger = (
        trend_buy
        and near_ema
        and not opposite_impulse_buy
        and not buy_chasing_impulse
        and prev_close <= prev_ema + 0.25
        and close1 > ema1
        and float(last1["rsi14"]) >= 52.0
        and close1 > float(last1["open"])
        and body >= max(0.12, atr1 * 0.15)
    )
    sell_trigger = (
        trend_sell
        and near_ema
        and not opposite_impulse_sell
        and not sell_chasing_impulse
        and prev_close >= prev_ema - 0.25
        and close1 < ema1
        and float(last1["rsi14"]) <= 48.0
        and close1 < float(last1["open"])
        and body >= max(0.12, atr1 * 0.15)
    )
    if trigger_mode == "range_reversion":
        max_adx = _env_float("XAU_SCALP_RANGE_MAX_M5_ADX", 18.0)
        max_ema_separation = _env_float("XAU_SCALP_RANGE_MAX_EMA_SEPARATION_ATR", 0.55)
        rsi_edge = _env_float("XAU_SCALP_RANGE_RSI_EDGE", 35.0)
        ema_separation = abs(float(last5["ema20"]) - float(last5["ema50"])) / max(atr5, 0.00001)
        range_regime = float(last5["adx14"]) <= max_adx and ema_separation <= max_ema_separation
        lower_wick = min(float(last1["open"]), close1) - float(last1["low"])
        upper_wick = float(last1["high"]) - max(float(last1["open"]), close1)
        buy_trigger = (
            range_regime
            and float(last1["low"]) <= float(last1["bb_lower"])
            and close1 > float(last1["bb_lower"])
            and float(last1["rsi14"]) <= rsi_edge
            and close1 > float(last1["open"])
            and lower_wick >= body * 0.35
        )
        sell_trigger = (
            range_regime
            and float(last1["high"]) >= float(last1["bb_upper"])
            and close1 < float(last1["bb_upper"])
            and float(last1["rsi14"]) >= (100.0 - rsi_edge)
            and close1 < float(last1["open"])
            and upper_wick >= body * 0.35
        )
    elif trigger_mode == "range_breakout":
        min_adx = _env_float("XAU_SCALP_BREAKOUT_MIN_M5_ADX", 16.0)
        min_body_atr = _env_float("XAU_SCALP_BREAKOUT_MIN_BODY_ATR", 0.55)
        min_volume_ratio = _env_float("XAU_SCALP_BREAKOUT_MIN_VOLUME_RATIO", 1.05)
        volume_ratio = float(last1["tick_volume"]) / max(float(last1["volume_ma20"]), 1.0)
        buy_trigger = (
            float(last5["adx14"]) >= min_adx
            and float(last15["ema20"]) > float(last15["ema50"])
            and close1 > float(last1["hh20"])
            and close1 > float(last1["open"])
            and body >= atr1 * min_body_atr
            and volume_ratio >= min_volume_ratio
            and not buy_chasing_impulse
        )
        sell_trigger = (
            float(last5["adx14"]) >= min_adx
            and float(last15["ema20"]) < float(last15["ema50"])
            and close1 < float(last1["ll20"])
            and close1 < float(last1["open"])
            and body >= atr1 * min_body_atr
            and volume_ratio >= min_volume_ratio
            and not sell_chasing_impulse
        )
    elif trigger_mode in EXTRA_SETUP_MODES:
        buy_trigger, sell_trigger, extra_diagnostics = evaluate_extra_setup(
            trigger_mode, last1, prev1, last5, prev5, last15, atr1, atr5, recent_move, prev2, prev3
        )
        diagnostics.update(extra_diagnostics)
    diagnostics["trigger_mode"] = trigger_mode

    if _env_bool("XAU_SCALP_QUALITY_FILTER_ENABLED", False):
        candle_range = max(0.00001, float(last1["high"]) - float(last1["low"]))
        close_location = (close1 - float(last1["low"])) / candle_range
        ema_separation_atr = abs(float(last5["ema20"]) - float(last5["ema50"])) / max(atr5, 0.00001)
        ema20_slope_atr = abs(float(last5["ema20"]) - float(prev5["ema20"])) / max(atr5, 0.00001)
        min_close_location = _env_float("XAU_SCALP_QUALITY_MIN_CLOSE_LOCATION", 0.65)
        min_ema_separation = _env_float("XAU_SCALP_QUALITY_MIN_EMA_SEPARATION_ATR", 0.15)
        min_ema_slope = _env_float("XAU_SCALP_QUALITY_MIN_EMA_SLOPE_ATR", 0.03)
        structure_ok = ema_separation_atr >= min_ema_separation and ema20_slope_atr >= min_ema_slope
        buy_quality = structure_ok and close_location >= min_close_location
        sell_quality = structure_ok and close_location <= (1.0 - min_close_location)
        buy_trigger = buy_trigger and buy_quality
        sell_trigger = sell_trigger and sell_quality
        diagnostics.update(
            {
                "quality_filter": True,
                "close_location": round(close_location, 4),
                "ema_separation_atr": round(ema_separation_atr, 4),
                "ema20_slope_atr": round(ema20_slope_atr, 4),
                "buy_quality": buy_quality,
                "sell_quality": sell_quality,
            }
        )
    if not buy_trigger and not sell_trigger:
        diagnostics["reason"] = "no_scalp_trigger"
        diagnostics["trend_buy"] = trend_buy
        diagnostics["trend_sell"] = trend_sell
        diagnostics["opposite_impulse_buy"] = opposite_impulse_buy
        diagnostics["opposite_impulse_sell"] = opposite_impulse_sell
        if trend_buy and buy_chasing_impulse:
            diagnostics["reason"] = "blocked_buy_chasing_impulse"
        elif trend_sell and sell_chasing_impulse:
            diagnostics["reason"] = "blocked_sell_chasing_impulse"
        return None, diagnostics

    side = "buy" if buy_trigger else "sell"
    entry = _side_price(symbol, side)
    indicator_profile = diagnostics.get("indicator_exit_profile") or INDICATOR_SETUP_PROFILES.get(trigger_mode)
    if indicator_profile:
        profile_atr = atr5 if str(indicator_profile["timeframe"]) == "M5" else atr1
        sl_dist = min(12.0, max(1.50, profile_atr * float(indicator_profile["sl_atr"])))
        tp1_dist = max(1.0, sl_dist * float(indicator_profile["tp_r"]))
        tp2_dist = tp1_dist
        be_trigger_dist = sl_dist * float(indicator_profile["be_r"])
        be_buffer_dist = float(indicator_profile["be_buffer"])
        max_hold_minutes = float(indicator_profile["hold_minutes"])
        level_mode = "indicator_research_profile"
    elif _env_bool("XAU_SCALP_ADAPTIVE_LEVELS_ENABLED", False):
        min_sl = _env_float("XAU_SCALP_ADAPTIVE_MIN_SL_USD", 2.8)
        min_tp1 = _env_float("XAU_SCALP_ADAPTIVE_MIN_TP1_USD", 0.8)
        min_tp2 = _env_float("XAU_SCALP_ADAPTIVE_MIN_TP2_USD", min_tp1 + 0.4)
        sl_mult = _env_float("XAU_SCALP_ADAPTIVE_SL_ATR_MULT", 2.1)
        tp1_mult = _env_float("XAU_SCALP_ADAPTIVE_TP1_ATR_MULT", 1.05)
        tp2_mult = _env_float("XAU_SCALP_ADAPTIVE_TP2_ATR_MULT", 1.7)
        sl_dist = min(float(cfg.xau_scalp_sl_usd), max(min_sl, atr1 * sl_mult, atr5 * 0.45))
        tp1_dist = max(min_tp1, min(float(cfg.xau_scalp_tp1_usd), max(atr1 * tp1_mult, atr5 * 0.22)))
        tp2_dist = max(tp1_dist + 0.4, min(float(cfg.xau_scalp_tp2_usd), max(min_tp2, atr1 * tp2_mult, atr5 * 0.36)))
        be_trigger_dist = float(getattr(cfg, "xau_scalp_be_trigger_usd", 0.0) or 0.0)
        be_buffer_dist = float(cfg.xau_scalp_be_buffer_usd)
        max_hold_minutes = 0.0
        level_mode = "adaptive_atr"
    else:
        sl_dist = min(float(cfg.xau_scalp_sl_usd), max(3.0, atr1 * 2.2))
        tp1_dist = float(cfg.xau_scalp_tp1_usd)
        tp2_dist = float(cfg.xau_scalp_tp2_usd)
        be_trigger_dist = float(getattr(cfg, "xau_scalp_be_trigger_usd", 0.0) or 0.0)
        be_buffer_dist = float(cfg.xau_scalp_be_buffer_usd)
        max_hold_minutes = 0.0
        level_mode = "fixed"
    base_sl_dist = sl_dist
    sl_dist, sl_distance_multiplier = _scaled_initial_sl_distance(base_sl_dist)
    base_tp1_dist = tp1_dist
    base_tp2_dist = tp2_dist
    tp1_dist, tp_distance_multiplier = _scaled_tp_distance(base_tp1_dist)
    tp2_dist, _ = _scaled_tp_distance(base_tp2_dist)
    be_trigger_multiplier = min(
        2.0,
        max(0.0, _env_float("XAU_SCALP_BE_TRIGGER_MULTIPLIER", 1.0)),
    )
    be_trigger_dist *= be_trigger_multiplier
    if side == "buy":
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
        tp2 = entry + tp2_dist
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
        tp2 = entry - tp2_dist
    signal_bar_source = last5 if indicator_profile and str(indicator_profile["timeframe"]) == "M5" else last1
    return (
        {
            "strategy": _strategy_name(),
            "setup_tag": str(diagnostics.get("setup_tag", "") or _strategy_name()),
            "signal_bar_utc": pd.Timestamp(signal_bar_source["time"]).isoformat(),
            "side": side,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "atr1": round(atr1, 4),
            "atr5": round(atr5, 4),
            "level_mode": level_mode,
            "base_sl_dist": round(base_sl_dist, 3),
            "sl_distance_multiplier": round(sl_distance_multiplier, 3),
            "sl_dist": round(sl_dist, 3),
            "base_tp1_dist": round(base_tp1_dist, 3),
            "base_tp2_dist": round(base_tp2_dist, 3),
            "tp_distance_multiplier": round(tp_distance_multiplier, 3),
            "tp1_dist": round(tp1_dist, 3),
            "tp2_dist": round(tp2_dist, 3),
            "be_enabled": be_trigger_dist > 0.0,
            "be_trigger_usd": round(be_trigger_dist, 3),
            "be_trigger_multiplier": round(be_trigger_multiplier, 3),
            "be_buffer_usd": round(be_buffer_dist, 3),
            "max_hold_minutes": round(max_hold_minutes, 1),
            "reason": str(diagnostics.get("selected_setup", "") or trigger_mode),
        },
        diagnostics,
    )


def _protect_positions(symbol: str, cfg, meta: dict, events_path: Path) -> bool:
    changed = False
    active_positions = sorted(
        positions_by_magic(symbol, int(cfg.xau_scalp_magic)),
        key=lambda position: int(getattr(position, "ticket", 0) or 0),
    )
    protect_leg_count = max(0, int(_env_float("XAU_SCALP_PROTECT_LEG_COUNT", len(active_positions))))
    for position_index, position in enumerate(active_positions):
        ticket = str(int(getattr(position, "ticket", 0) or 0))
        item = meta.setdefault(ticket, {})
        if _backfill_position_meta(position, cfg, item):
            _append_jsonl(events_path, {"type": "position_meta_backfilled", "ticket": int(ticket), "source": "protect"})
            changed = True
        if _hold_remaining_seconds(position, item) > 0:
            continue
        side = "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell"
        entry = float(getattr(position, "price_open", 0.0) or 0.0)
        current = float(getattr(position, "price_current", 0.0) or 0.0)
        tp1 = float(item.get("tp1", 0.0) or 0.0)
        if position_index >= protect_leg_count or tp1 <= 0:
            continue
        if item.get("be_enabled") is False:
            continue
        trigger_dist = float(item.get("be_trigger_usd", getattr(cfg, "xau_scalp_be_trigger_usd", 0.0)) or 0.0)
        if trigger_dist <= 0.0:
            continue
        trigger_price = entry + trigger_dist if side == "buy" else entry - trigger_dist
        reached = current >= trigger_price if side == "buy" else current <= trigger_price
        if not reached:
            continue
        trail_sl = _trailing_stop_candidate(side, entry, current, cfg)
        be_buffer = float(item.get("be_buffer_usd", cfg.xau_scalp_be_buffer_usd) or 0.0)
        be_sl = entry + be_buffer if side == "buy" else entry - be_buffer
        new_sl = trail_sl if trail_sl is not None else be_sl
        old_sl = float(getattr(position, "sl", 0.0) or 0.0)
        better = new_sl > old_sl if side == "buy" else old_sl <= 0 or new_sl < old_sl
        if not better:
            item["be_done"] = True
            changed = True
            continue
        result = modify_position(position, round(new_sl, 2), float(getattr(position, "tp", 0.0) or 0.0))
        retcode = getattr(result, "retcode", None)
        _append_jsonl(
            events_path,
            {
                "type": "protect_trail" if trail_sl is not None else "protect_be",
                "ticket": int(ticket),
                "new_sl": round(new_sl, 2),
                "retcode": retcode,
            },
        )
        if retcode in {10008, 10009}:
            item["be_done"] = True
            changed = True
    return changed


def _manage_indicator_time_exits(symbol: str, cfg, meta: dict, events_path: Path) -> bool:
    changed = False
    for position in positions_by_magic(symbol, int(cfg.xau_scalp_magic)):
        ticket = str(int(getattr(position, "ticket", 0) or 0))
        item = meta.setdefault(ticket, {})
        if _backfill_position_meta(position, cfg, item):
            changed = True
        max_hold_minutes = float(item.get("max_hold_minutes", 0.0) or 0.0)
        if max_hold_minutes <= 0.0 or _position_age_seconds(position) < max_hold_minutes * 60.0:
            continue
        result = close_position(position, int(cfg.deviation))
        retcode = getattr(result, "retcode", None)
        _append_jsonl(
            events_path,
            {
                "type": "indicator_max_hold_exit",
                "ticket": int(ticket),
                "strategy": item.get("strategy", _strategy_name()),
                "age_seconds": round(_position_age_seconds(position), 1),
                "max_hold_minutes": max_hold_minutes,
                "retcode": retcode,
            },
        )
        changed = True
    return changed


def _martingale_triggered(side: str, entry: float, current: float, adverse_usd: float) -> bool:
    if side == "buy":
        return current <= entry - adverse_usd
    if side == "sell":
        return current >= entry + adverse_usd
    return False


def _manage_martingale_step(
    symbol: str,
    cfg,
    meta: dict,
    runtime: dict,
    runtime_path: Path,
    events_path: Path,
) -> bool:
    if not _env_bool("XAU_SCALP_MARTINGALE_ENABLED", False):
        return False
    signal_bar = str(runtime.get("last_opened_signal_bar_utc", "") or "")
    if not signal_bar or str(runtime.get("martingale_armed_signal_bar_utc", "") or "") != signal_bar:
        return False
    if str(runtime.get("martingale_step_signal_bar_utc", "") or "") == signal_bar:
        return False

    positions = sorted(
        positions_by_magic(symbol, int(cfg.xau_scalp_magic)),
        key=lambda position: int(getattr(position, "ticket", 0) or 0),
    )
    primary = [position for position in positions if "MG1" not in str(getattr(position, "comment", "") or "")]
    add_legs = max(1, min(3, int(_env_float("XAU_SCALP_MARTINGALE_ADD_LEGS", 3))))
    max_positions = max(add_legs + 1, int(_env_float("XAU_SCALP_MARTINGALE_MAX_POSITIONS", 6)))
    if len(primary) < 3 or len(positions) + add_legs > max_positions:
        return False

    first = primary[0]
    side = "buy" if int(getattr(first, "type", 0) or 0) == 0 else "sell"
    if any(("buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell") != side for position in primary):
        return False
    entry = sum(float(getattr(position, "price_open", 0.0) or 0.0) for position in primary) / len(primary)
    tick = get_tick(symbol)
    current = float(tick.bid if side == "buy" else tick.ask)
    adverse_usd = max(0.1, _env_float("XAU_SCALP_MARTINGALE_ADVERSE_USD", 1.5))
    if not _martingale_triggered(side, entry, current, adverse_usd):
        return False

    runtime["martingale_step_signal_bar_utc"] = signal_bar
    runtime["martingale_step_state"] = "opening"
    _write_json(runtime_path, runtime)
    before_tickets = {int(getattr(position, "ticket", 0) or 0) for position in positions}
    opened: list[dict] = []
    source_positions = primary[:add_legs]
    for index, source in enumerate(source_positions, start=1):
        source_ticket = str(int(getattr(source, "ticket", 0) or 0))
        source_meta = meta.get(source_ticket, {}) if isinstance(meta.get(source_ticket), dict) else {}
        sl = float(source_meta.get("sl", getattr(source, "sl", 0.0)) or 0.0)
        tp = float(source_meta.get("tp1", getattr(source, "tp", 0.0)) or 0.0)
        volume = float(getattr(source, "volume", 0.0) or 0.0)
        result = send_market_order(
            symbol=symbol,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            deviation=cfg.deviation,
            magic=int(cfg.xau_scalp_magic),
            comment=f"XAU-MG1:{index}",
        )
        opened.append(
            {
                "leg": f"mg1_{index}",
                "volume": volume,
                "sl": sl,
                "tp": tp,
                "retcode": getattr(result, "retcode", None),
            }
        )

    time.sleep(0.5)
    for position in positions_by_magic(symbol, int(cfg.xau_scalp_magic)):
        ticket_int = int(getattr(position, "ticket", 0) or 0)
        if ticket_int in before_tickets:
            continue
        ticket = str(ticket_int)
        position_side = "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell"
        meta[ticket] = {
            "created_utc": datetime.now(UTC).isoformat(),
            "min_hold_seconds": 0.0,
            "strategy": f"{_strategy_name()}-MG1",
            "leg": "mg1",
            "side": position_side,
            "volume": float(getattr(position, "volume", 0.0) or 0.0),
            "entry": float(getattr(position, "price_open", 0.0) or 0.0),
            "tp1": float(getattr(position, "tp", 0.0) or 0.0),
            "tp2": float(getattr(position, "tp", 0.0) or 0.0),
            "sl": float(getattr(position, "sl", 0.0) or 0.0),
            "be_done": False,
            "martingale_step": 1,
            "source_signal_bar_utc": signal_bar,
        }
    accepted = sum(1 for item in opened if item["retcode"] in {10008, 10009})
    runtime["martingale_step_state"] = "opened" if accepted else "rejected"
    runtime["martingale_step_accepted"] = accepted
    _append_jsonl(
        events_path,
        {
            "type": "martingale_step_1",
            "signal_bar_utc": signal_bar,
            "side": side,
            "average_entry": round(entry, 2),
            "trigger_price": round(current, 2),
            "adverse_usd": adverse_usd,
            "opened": opened,
        },
    )
    return True


def _manage_min_hold_exits(symbol: str, cfg, meta: dict, events_path: Path) -> bool:
    changed = False
    for position in positions_by_magic(symbol, int(cfg.xau_scalp_magic)):
        ticket = str(int(getattr(position, "ticket", 0) or 0))
        item = meta.setdefault(ticket, {})
        if _backfill_position_meta(position, cfg, item):
            _append_jsonl(events_path, {"type": "position_meta_backfilled", "ticket": int(ticket), "source": "min_hold"})
            changed = True
        remaining = _hold_remaining_seconds(position, item)
        if remaining > 0:
            continue
        side = "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell"
        current = float(getattr(position, "price_current", 0.0) or 0.0)
        intended_sl = float(item.get("sl", 0.0) or 0.0)
        leg = str(item.get("leg", "tp1") or "tp1")
        intended_tp = float(item.get("intended_tp", 0.0) or 0.0)
        if intended_tp <= 0.0:
            intended_tp = float(item.get("tp2" if "tp2" in leg else "tp1", 0.0) or 0.0)
        if intended_sl <= 0.0 or intended_tp <= 0.0:
            if not item.get("missing_levels_logged"):
                _append_jsonl(
                    events_path,
                    {
                        "type": "min_hold_missing_levels",
                        "ticket": int(ticket),
                        "position_sl": float(getattr(position, "sl", 0.0) or 0.0),
                        "position_tp": float(getattr(position, "tp", 0.0) or 0.0),
                        "comment": str(getattr(position, "comment", "") or ""),
                    },
                )
                item["missing_levels_logged"] = True
                changed = True
            continue
        hit_tp = intended_tp > 0 and (current >= intended_tp if side == "buy" else current <= intended_tp)
        hit_sl = intended_sl > 0 and (current <= intended_sl if side == "buy" else current >= intended_sl)
        if hit_tp or hit_sl:
            result = close_position(position, int(cfg.deviation))
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "min_hold_manual_exit_attempt",
                    "ticket": int(ticket),
                    "reason": "tp" if hit_tp else "sl",
                    "current": current,
                    "intended_sl": intended_sl,
                    "intended_tp": intended_tp,
                    "age_seconds": round(_position_age_seconds(position), 1),
                    "min_hold_seconds": round(float(item.get("min_hold_seconds", 0.0) or 0.0), 1),
                    "retcode": retcode,
                },
            )
            changed = True
            continue
        if _delay_broker_levels_for_min_hold() and (float(getattr(position, "sl", 0.0) or 0.0) <= 0 or float(getattr(position, "tp", 0.0) or 0.0) <= 0):
            result = modify_position(position, round(intended_sl, 2), round(intended_tp, 2))
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "min_hold_levels_armed",
                    "ticket": int(ticket),
                    "sl": round(intended_sl, 2),
                    "tp": round(intended_tp, 2),
                    "retcode": retcode,
                },
            )
            changed = True
    return changed


def _record_closed(symbol: str, cfg, meta: dict, trades_path: Path, events_path: Path) -> bool:
    live = {str(int(getattr(position, "ticket", 0) or 0)) for position in positions_by_magic(symbol, int(cfg.xau_scalp_magic))}
    changed = False
    for ticket, item in list(meta.items()):
        if ticket in live or item.get("closed"):
            continue
        profit, gross_profit, costs, exit_price = _closed_position_summary(ticket, item)
        _append_trade(
            trades_path,
            {
                "ticket": ticket,
                "side": item.get("side", ""),
                "volume": item.get("volume", 0.0),
                "entry": item.get("entry", 0.0),
                "exit": exit_price,
                "profit": round(profit, 2),
                "gross_profit": round(gross_profit, 2),
                "costs": round(costs, 2),
                "reason": "mt5_position_closed",
                "strategy": item.get("strategy", _strategy_name()),
            },
        )
        item["closed"] = True
        item["closed_utc"] = datetime.now(UTC).isoformat()
        item["closed_profit"] = round(profit, 2)
        _append_jsonl(events_path, {"type": "closed", "ticket": ticket, "profit": round(profit, 2)})
        changed = True
    return changed


def run() -> None:
    cfg = load_settings()
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    status_path = data_dir / STATUS_FILE
    events_path = data_dir / EVENTS_FILE
    trades_path = data_dir / TRADES_FILE
    meta_path = data_dir / META_FILE
    runtime_path = data_dir / RUNTIME_FILE
    if not bool(cfg.xau_scalp_enabled):
        _write_json(
            status_path,
            {
                "heartbeat_utc": datetime.now(UTC).isoformat(),
                "enabled": False,
                "running": False,
                "strategy": _strategy_name(),
                "reason": "disabled_by_profile",
                "positions_count": 0,
            },
        )
        return
    _ensure_trades(trades_path)

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    sync_role = _sync_role()
    sync_path = _sync_file(cfg)
    runtime = _read_json(runtime_path, {})
    meta = _read_json(meta_path, {})
    account = account_info()
    day_key = runtime.get("day_key") or trading_day_key()
    day_start_equity = float(runtime.get("day_start_equity", float(account.equity)))
    if int(runtime.get("profit_lock_balance_schema", 0) or 0) < 1:
        day_start_balance = day_start_equity
        runtime["day_start_balance"] = day_start_balance
        runtime["profit_lock_balance_schema"] = 1
    else:
        day_start_balance = float(runtime.get("day_start_balance", float(account.balance)))
    start_equity = float(runtime.get("start_equity", float(account.equity)))
    runtime.setdefault("start_equity", start_equity)

    try:
        while True:
            cfg = load_settings()
            now = datetime.now(UTC)
            if trading_day_key() != day_key:
                day_key = trading_day_key()
                fresh_account = account_info()
                day_start_equity = float(fresh_account.equity)
                day_start_balance = float(fresh_account.balance)
            account = account_info()
            equity = float(account.equity)
            expected_login = int(getattr(cfg, "mt5_login", 0) or 0)
            actual_login = int(getattr(account, "login", 0) or 0)
            account_mismatch = expected_login > 0 and actual_login != expected_login
            daily_dd = max(0.0, ((day_start_equity - equity) / day_start_equity) * 100.0) if day_start_equity > 0 else 0.0
            day_profit = equity - day_start_equity
            profit_locked, profit_lock_status = _update_daily_profit_lock(
                runtime,
                str(day_key),
                day_profit,
                day_start_balance,
                events_path,
            )
            total_dd = max(0.0, ((start_equity - equity) / start_equity) * 100.0) if start_equity > 0 else 0.0
            positions = positions_by_magic(symbol, int(cfg.xau_scalp_magic))
            spread = current_spread_points(symbol)
            session_status = _session_filter_status(now)
            manage_only = _env_bool("XAU_SCALP_MANAGE_ONLY", False)

            if (
                _manage_min_hold_exits(symbol, cfg, meta, events_path)
                or _manage_indicator_time_exits(symbol, cfg, meta, events_path)
                or _protect_positions(symbol, cfg, meta, events_path)
                or _manage_martingale_step(symbol, cfg, meta, runtime, runtime_path, events_path)
                or _record_closed(symbol, cfg, meta, trades_path, events_path)
            ):
                _write_json(meta_path, meta)
                if _loss_pause_enabled() and _recent_full_loss_batch(trades_path):
                    pause_until = time.time() + 60 * 60
                    runtime["loss_pause_until_epoch"] = pause_until
                    _append_jsonl(
                        events_path,
                        {
                            "type": "loss_pause_started",
                            "reason": "one_full_losing_batch",
                            "pause_seconds": 3600,
                        },
                    )

            if sync_role == "follower":
                signal, diagnostics = _read_sync_signal(sync_path, symbol, runtime)
            else:
                signal, diagnostics = _signal_from_market(symbol, cfg)
            if signal:
                direction_block_remaining = _direction_loss_block_remaining(trades_path, str(signal.get("side", "") or ""))
                diagnostics["direction_loss_block_remaining_seconds"] = round(direction_block_remaining, 1)
                if direction_block_remaining > 0:
                    diagnostics["blocked"] = "direction_loss_block"
                    signal = None
            if signal:
                side_quality_remaining, side_quality_status = _side_quality_block_remaining(trades_path, str(signal.get("side", "") or ""))
                diagnostics["side_quality"] = side_quality_status
                if side_quality_remaining > 0:
                    diagnostics["blocked"] = "side_quality_block"
                    signal = None
            spread_ok, spread_quality = _spread_quality_ok(spread, signal, cfg)
            diagnostics["spread_quality"] = spread_quality
            can_trade = (
                bool(cfg.xau_scalp_enabled)
                and not manage_only
                and signal is not None
                and not account_mismatch
                and spread_ok
                and bool(session_status.get("active", True))
                and not profit_locked
                and daily_dd < float(cfg.xau_scalp_daily_dd_pct)
                and total_dd < float(cfg.xau_scalp_total_dd_pct)
                and (
                    int(cfg.xau_scalp_max_positions) <= 0
                    or len(positions) < int(cfg.xau_scalp_max_positions)
                )
            )
            terminal_status = trading_status()
            diagnostics["terminal"] = terminal_status
            if not terminal_status["ready"]:
                can_trade = False
                diagnostics["blocked"] = "mt5_not_ready:" + ",".join(terminal_status["blocked_reasons"])
            single_active_batch = _env_bool("XAU_SCALP_SINGLE_ACTIVE_BATCH_ENABLED", True)
            if single_active_batch and positions:
                can_trade = False
                diagnostics["blocked"] = "active_scalp_batch"
            if not bool(session_status.get("active", True)):
                diagnostics["blocked"] = "outside_active_session"
                diagnostics["session_filter"] = session_status
            if not spread_ok:
                diagnostics["blocked"] = spread_quality.get("reason") or "spread_filter"
            if profit_locked:
                diagnostics["blocked"] = profit_lock_status.get("reason") or "daily_profit_locked"
            if account_mismatch:
                diagnostics["blocked"] = "mt5_account_mismatch"
                diagnostics["expected_login"] = expected_login
                diagnostics["actual_login"] = actual_login
            if manage_only:
                diagnostics["blocked"] = "manage_only"
            last_trade_epoch = float(runtime.get("last_trade_epoch", 0.0) or 0.0)
            if time.time() - last_trade_epoch < int(cfg.xau_scalp_cooldown_seconds):
                can_trade = False
                diagnostics["blocked"] = "cooldown"
            loss_pause_until = float(runtime.get("loss_pause_until_epoch", 0.0) or 0.0)
            if _loss_pause_enabled() and time.time() < loss_pause_until:
                can_trade = False
                diagnostics["blocked"] = "loss_pause_after_two_bad_batches"

            signal_bar = str(signal.get("signal_bar_utc", "") or "") if signal else ""
            if signal_bar and signal_bar == str(runtime.get("last_opened_signal_bar_utc", "") or ""):
                can_trade = False
                diagnostics["blocked"] = "signal_bar_already_traded"

            if can_trade and signal:
                if sync_role == "master":
                    try:
                        _publish_sync_signal(sync_path, signal, cfg, symbol, diagnostics)
                    except Exception as exc:
                        diagnostics["sync_publish_error"] = type(exc).__name__
                total_lot, dynamic_steps = _xau_scalp_total_lot(symbol, cfg, float(account.balance), runtime)
                legs = _xau_scalp_legs(signal)
                scalp_risk_base = _account_risk_base(account)
                risk_leg_lot = _xau_scalp_risk_leg_lot(symbol, signal, scalp_risk_base, cfg, len(legs))
                if risk_leg_lot is not None:
                    leg_lot = risk_leg_lot
                    total_lot = normalize_volume(
                        symbol,
                        leg_lot * max(1, len(legs)),
                        float(cfg.min_lot),
                        float(cfg.max_lot),
                    )
                else:
                    leg_lot = normalize_volume(symbol, total_lot / max(1, len(legs)), float(cfg.min_lot), float(cfg.max_lot))
                opened = []
                for leg, tp in legs:
                    result = send_market_order(
                        symbol=symbol,
                        side=signal["side"],
                        volume=leg_lot,
                        sl=0.0 if _delay_broker_levels_for_min_hold() else signal["sl"],
                        tp=0.0 if _delay_broker_levels_for_min_hold() else tp,
                        deviation=cfg.deviation,
                        magic=int(cfg.xau_scalp_magic),
                        comment=_strategy_comment(leg, signal),
                    )
                    retcode = getattr(result, "retcode", None)
                    ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
                    opened.append({"leg": leg, "tp": tp, "retcode": retcode, "ticket": ticket})
                    _append_jsonl(
                        events_path,
                        {
                            "type": "open_attempt",
                            "signal": signal,
                            "leg": leg,
                            "volume": leg_lot,
                            "total_lot": total_lot,
                            "dynamic_lot_enabled": bool(getattr(cfg, "xau_scalp_dynamic_lot_enabled", False)),
                            "dynamic_steps": dynamic_steps,
                            "tp": tp,
                            "retcode": retcode,
                        },
                    )
                time.sleep(0.5)
                for position in positions_by_magic(symbol, int(cfg.xau_scalp_magic)):
                    ticket = str(int(getattr(position, "ticket", 0) or 0))
                    existing = meta.get(ticket)
                    if (
                        isinstance(existing, dict)
                        and existing.get("strategy")
                        and float(existing.get("tp1", 0.0) or 0.0) > 0.0
                        and float(existing.get("sl", 0.0) or 0.0) > 0.0
                        and not existing.get("closed")
                    ):
                        continue
                    comment = str(getattr(position, "comment", "") or "")
                    matched_leg = next(
                        ((name, target) for name, target in _xau_scalp_legs(signal) if name in comment),
                        None,
                    )
                    leg = matched_leg[0] if matched_leg else ("tp2" if "tp2" in comment else "tp1")
                    meta[ticket] = {
                        "created_utc": datetime.now(UTC).isoformat(),
                        "min_hold_seconds": _min_hold_seconds(),
                        "strategy": str(signal.get("setup_tag", "") or _strategy_name()),
                        "leg": leg,
                        "side": "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell",
                        "volume": float(getattr(position, "volume", 0.0) or 0.0),
                        "entry": float(getattr(position, "price_open", 0.0) or 0.0),
                        "tp1": float(signal["tp1"]),
                        "tp2": float(signal["tp2"]),
                        "intended_tp": float(matched_leg[1] if matched_leg else getattr(position, "tp", 0.0) or 0.0),
                        "sl": float(signal["sl"]),
                        "be_done": False,
                        "be_enabled": bool(signal.get("be_enabled", True)),
                        "be_trigger_usd": float(signal.get("be_trigger_usd", getattr(cfg, "xau_scalp_be_trigger_usd", 0.0)) or 0.0),
                        "be_buffer_usd": float(signal.get("be_buffer_usd", cfg.xau_scalp_be_buffer_usd) or 0.0),
                        "max_hold_minutes": float(signal.get("max_hold_minutes", 0.0) or 0.0),
                    }
                accepted = any(item.get("retcode") in {10008, 10009} for item in opened)
                if accepted:
                    runtime["last_trade_epoch"] = time.time()
                    if signal_bar:
                        runtime["last_opened_signal_bar_utc"] = signal_bar
                        runtime["martingale_armed_signal_bar_utc"] = signal_bar
                        runtime["martingale_step_signal_bar_utc"] = ""
                        runtime["martingale_step_state"] = "armed"
                else:
                    retry_after = 10.0
                    runtime["last_trade_epoch"] = time.time() - max(0.0, float(cfg.xau_scalp_cooldown_seconds) - retry_after)
                    _append_jsonl(
                        events_path,
                        {
                            "type": "open_batch_rejected",
                            "signal": signal,
                            "opened": opened,
                            "retry_after_seconds": retry_after,
                        },
                    )
                runtime["last_signal"] = signal
                runtime["last_opened"] = opened
                if accepted and sync_role == "follower" and signal.get("sync_id"):
                    runtime["last_sync_signal_id"] = str(signal.get("sync_id"))
                _write_json(meta_path, meta)

            positions = positions_by_magic(symbol, int(cfg.xau_scalp_magic))
            open_profit = sum(float(getattr(position, "profit", 0.0) or 0.0) for position in positions)
            day_closed_profit = _daily_closed_profit(trades_path)
            side_stats = _daily_side_stats(trades_path)
            status_total_lot, status_dynamic_steps = _xau_scalp_total_lot(symbol, cfg, float(account.balance), runtime)
            status_leg_count = _xau_scalp_leg_count()
            scalp_risk_base = _account_risk_base(account)
            status_risk_leg_lot = _xau_scalp_risk_leg_lot(symbol, signal, scalp_risk_base, cfg, status_leg_count)
            if status_risk_leg_lot is not None:
                status_leg_lot = status_risk_leg_lot
                status_total_lot = normalize_volume(
                    symbol,
                    status_leg_lot * max(1, status_leg_count),
                    float(cfg.min_lot),
                    float(cfg.max_lot),
                )
            else:
                status_leg_lot = normalize_volume(symbol, status_total_lot / max(1, status_leg_count), float(cfg.min_lot), float(cfg.max_lot))
            runtime.update(
                {
                    "day_key": day_key,
                    "day_start_equity": day_start_equity,
                    "day_start_balance": day_start_balance,
                    "last_heartbeat_utc": now.isoformat(),
                }
            )
            _write_json(runtime_path, runtime)
            _write_json(
                status_path,
                {
                    "heartbeat_utc": now.isoformat(),
                    "terminal": terminal_status,
                    "enabled": bool(cfg.xau_scalp_enabled),
                    "symbol": symbol,
                    "magic": int(cfg.xau_scalp_magic),
                    "strategy": _strategy_name(),
                    "account": {
                        "login": int(account.login),
                        "server": str(account.server),
                        "balance": float(account.balance),
                        "equity": float(account.equity),
                        "profit": float(account.profit),
                        "expected_login": expected_login,
                        "login_matches_env": not account_mismatch,
                    },
                    "positions_count": len(positions),
                    "open_profit": round(open_profit, 2),
                    "day_realized_profit": round(day_closed_profit, 2),
                    "total_lot": status_total_lot,
                    "max_positions": int(cfg.xau_scalp_max_positions),
                    "manage_only": manage_only,
                    "max_spread_points": int(cfg.xau_scalp_max_spread_points),
                    "sl_usd": float(cfg.xau_scalp_sl_usd),
                    "sl_distance_multiplier": _scaled_initial_sl_distance(1.0)[1],
                    "tp_distance_multiplier": _scaled_tp_distance(1.0)[1],
                    "tp1_usd": float(cfg.xau_scalp_tp1_usd),
                    "tp2_usd": float(cfg.xau_scalp_tp2_usd),
                    "be_trigger_usd": float(getattr(cfg, "xau_scalp_be_trigger_usd", cfg.xau_scalp_tp1_usd)),
                    "cooldown_seconds": int(cfg.xau_scalp_cooldown_seconds),
                    "last_signal_side": signal.get("side") if signal else None,
                    "last_reason": diagnostics.get("reason") or diagnostics.get("blocked") or "ready",
                    "sync": {
                        "role": sync_role or "independent",
                        "file": str(sync_path),
                        "ttl_seconds": _sync_ttl_seconds(),
                        "last_sync_signal_id": str(runtime.get("last_sync_signal_id", "") or ""),
                    },
                    "risk": {
                        "spread_points": spread,
                        "daily_dd_pct": round(daily_dd, 4),
                        "total_dd_pct": round(total_dd, 4),
                        "max_daily_dd_pct": float(cfg.xau_scalp_daily_dd_pct),
                        "max_total_dd_pct": float(cfg.xau_scalp_total_dd_pct),
                        "daily_closed_profit": round(day_closed_profit, 2),
                        "daily_profit_lock": profit_lock_status,
                        "side_stats": side_stats,
                        "session_filter": session_status,
                        "spread_quality": spread_quality,
                    },
                    "settings": {
                        "lot": status_total_lot,
                        "leg_lot": status_leg_lot,
                        "leg_count": status_leg_count,
                        "target_plan": _xau_scalp_target_plan(),
                        "target_r_plan": _xau_scalp_target_r_plan(),
                        "single_active_batch_enabled": _env_bool(
                            "XAU_SCALP_SINGLE_ACTIVE_BATCH_ENABLED", True
                        ),
                        "dynamic_lot_enabled": bool(getattr(cfg, "xau_scalp_dynamic_lot_enabled", False)),
                        "dynamic_steps": status_dynamic_steps,
                        "dynamic_lot_basis": str(os.getenv("XAU_SCALP_DYNAMIC_LOT_BASIS", "profit") or "profit"),
                        "dynamic_reference_id": str(runtime.get("dynamic_reference_id", "") or ""),
                        "dynamic_reference_balance": round(float(runtime.get("dynamic_reference_balance", account.balance) or account.balance), 2),
                        "dynamic_profit_usd": round(max(0.0, float(account.balance) - float(runtime.get("dynamic_reference_balance", account.balance) or account.balance)), 2),
                        "dynamic_step_usd": float(getattr(cfg, "xau_scalp_dynamic_step_usd", 1000.0) or 1000.0),
                        "dynamic_lot_add": float(getattr(cfg, "xau_scalp_dynamic_lot_add", 0.01) or 0.01),
                        "risk_pct_per_setup": max(0.0, _env_float("XAU_SCALP_RISK_PCT", 0.0)),
                        "tp1_usd": float(cfg.xau_scalp_tp1_usd),
                        "tp2_usd": float(cfg.xau_scalp_tp2_usd),
                        "sl_usd": float(cfg.xau_scalp_sl_usd),
                        "sl_distance_multiplier": _scaled_initial_sl_distance(1.0)[1],
                        "tp_distance_multiplier": _scaled_tp_distance(1.0)[1],
                        "be_trigger_usd": float(getattr(cfg, "xau_scalp_be_trigger_usd", cfg.xau_scalp_tp1_usd)),
                        "cooldown_seconds": int(cfg.xau_scalp_cooldown_seconds),
                        "adaptive_levels_enabled": _env_bool("XAU_SCALP_ADAPTIVE_LEVELS_ENABLED", False),
                        "session_filter_enabled": _env_bool("XAU_SCALP_SESSION_FILTER_ENABLED", False),
                        "closed_candle_only": _env_bool("XAU_SCALP_CLOSED_CANDLE_ONLY", False),
                        "quality_filter_enabled": _env_bool("XAU_SCALP_QUALITY_FILTER_ENABLED", False),
                        "side_quality_block_enabled": _env_bool("XAU_SCALP_SIDE_QUALITY_BLOCK_ENABLED", True),
                        "martingale_enabled": _env_bool("XAU_SCALP_MARTINGALE_ENABLED", False),
                        "martingale_adverse_usd": _env_float("XAU_SCALP_MARTINGALE_ADVERSE_USD", 1.5),
                        "martingale_add_legs": int(_env_float("XAU_SCALP_MARTINGALE_ADD_LEGS", 3)),
                        "martingale_max_steps": 1,
                    },
                    "signal": signal,
                    "diagnostics": diagnostics,
                    "positions": [_status_position(position) for position in positions],
                },
            )
            time.sleep(float(cfg.xau_scalp_loop_seconds))
    finally:
        shutdown()
