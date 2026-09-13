from __future__ import annotations

import csv
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import load_settings
from .indicators import enrich
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    connect,
    ensure_symbol,
    get_rates_df,
    get_tick,
    modify_position,
    mt5,
    positions_by_magic,
    send_market_order,
    shutdown,
)
from .risk import current_spread_points, normalize_volume, trading_day_key


STATUS_FILE = "btc_scalp_status.json"
EVENTS_FILE = "btc_scalp_events.jsonl"
TRADES_FILE = "btc_scalp_trades.csv"
META_FILE = "btc_scalp_meta.json"
RUNTIME_FILE = "btc_scalp_runtime.json"


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
    exit_price = float(item.get("entry", 0.0) or 0.0)
    closing_deals = [deal for deal in deals if int(getattr(deal, "entry", -1) or -1) != 0]
    if closing_deals:
        exit_price = float(getattr(closing_deals[-1], "price", exit_price) or exit_price)
    elif deals:
        exit_price = float(getattr(deals[-1], "price", exit_price) or exit_price)
    return gross + commission + swap, gross, commission + swap, exit_price


def _ensure_btc_symbol(preferred: str) -> str:
    candidates = [preferred, "BTCUSD", "BTCUSD+", "BTCUSDm", "BTCUSD."] 
    seen: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return ensure_symbol(candidate)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Nie udalo sie wybrac symbolu BTC: {last_error}")


def _side_price(symbol: str, side: str) -> float:
    tick = get_tick(symbol)
    return float(tick.ask if side == "buy" else tick.bid)


def _status_position(position) -> dict:
    comment = str(getattr(position, "comment", "") or "")
    return {
        "ticket": int(getattr(position, "ticket", 0) or 0),
        "symbol": str(getattr(position, "symbol", "")),
        "side": "BUY" if int(getattr(position, "type", 0) or 0) == 0 else "SELL",
        "leg": "tp2" if "tp2" in comment else "tp1",
        "volume": float(getattr(position, "volume", 0.0) or 0.0),
        "price_open": float(getattr(position, "price_open", 0.0) or 0.0),
        "current": float(getattr(position, "price_current", 0.0) or 0.0),
        "sl": float(getattr(position, "sl", 0.0) or 0.0),
        "tp": float(getattr(position, "tp", 0.0) or 0.0),
        "profit": float(getattr(position, "profit", 0.0) or 0.0),
        "comment": comment,
    }


def _daily_closed_profit(path: Path) -> float:
    today = datetime.now(UTC).date().isoformat()
    if not path.exists():
        return 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return sum(float(row.get("profit", 0.0) or 0.0) for row in csv.DictReader(handle) if str(row.get("closed_at_utc", "")).startswith(today))
    except Exception:
        return 0.0


def _signal_from_market(symbol: str, cfg) -> tuple[dict | None, dict]:
    m1 = enrich(get_rates_df(symbol, "M1", 260))
    m5 = enrich(get_rates_df(symbol, "M5", 260))
    m15 = enrich(get_rates_df(symbol, "M15", 260))
    if len(m1) < 90 or len(m5) < 90 or len(m15) < 90:
        return None, {"reason": "rates_not_ready"}

    last1 = m1.iloc[-1]
    prev1 = m1.iloc[-2]
    last5 = m5.iloc[-1]
    last15 = m15.iloc[-1]
    atr1 = float(last1.get("atr14", 0.0) or 0.0)
    atr5 = float(last5.get("atr14", atr1) or atr1)
    if atr1 <= 0.0 or atr5 <= 0.0:
        return None, {"reason": "atr_not_ready"}

    trend_buy = (
        float(last15["ema20"]) > float(last15["ema50"])
        and float(last5["ema20"]) > float(last5["ema50"])
        and float(last5["close"]) > float(last5["ema20"])
        and float(last5["adx14"]) >= 16.0
    )
    trend_sell = (
        float(last15["ema20"]) < float(last15["ema50"])
        and float(last5["ema20"]) < float(last5["ema50"])
        and float(last5["close"]) < float(last5["ema20"])
        and float(last5["adx14"]) >= 16.0
    )

    close1 = float(last1["close"])
    open1 = float(last1["open"])
    ema1 = float(last1["ema20"])
    prev_close = float(prev1["close"])
    prev_ema = float(prev1["ema20"])
    body = abs(close1 - open1)
    near_ema = abs(close1 - ema1) <= max(atr1 * 0.9, atr5 * 0.25)
    diagnostics = {
        "m1_close": close1,
        "m1_ema20": ema1,
        "m1_atr14": atr1,
        "m5_atr14": atr5,
        "m5_adx14": float(last5["adx14"]),
        "near_ema": near_ema,
    }

    buy_trigger = trend_buy and near_ema and prev_close <= prev_ema and close1 > ema1 and float(last1["rsi14"]) >= 53.0 and close1 > open1 and body >= atr1 * 0.20
    sell_trigger = trend_sell and near_ema and prev_close >= prev_ema and close1 < ema1 and float(last1["rsi14"]) <= 47.0 and close1 < open1 and body >= atr1 * 0.20
    if not buy_trigger and not sell_trigger:
        diagnostics.update({"reason": "no_scalp_trigger", "trend_buy": trend_buy, "trend_sell": trend_sell})
        return None, diagnostics

    side = "buy" if buy_trigger else "sell"
    entry = _side_price(symbol, side)
    sl_dist = max(atr1 * float(cfg.btc_scalp_sl_atr_mult), atr5 * 0.45)
    tp1_dist = max(atr1 * float(cfg.btc_scalp_tp1_atr_mult), atr5 * 0.20)
    tp2_dist = max(atr1 * float(cfg.btc_scalp_tp2_atr_mult), atr5 * 0.35)
    if side == "buy":
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
        tp2 = entry + tp2_dist
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
        tp2 = entry - tp2_dist
    return (
        {
            "strategy": "btc scalp",
            "side": side,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "atr1": round(atr1, 2),
            "atr5": round(atr5, 2),
            "be_trigger_dist": round(max(atr1 * float(cfg.btc_scalp_be_trigger_atr_mult), atr5 * 0.18), 2),
            "reason": "m15_m5_trend_m1_ema20_reclaim",
        },
        diagnostics,
    )


def _protect_positions(symbol: str, cfg, meta: dict, events_path: Path) -> bool:
    changed = False
    for position in positions_by_magic(symbol, int(cfg.btc_scalp_magic)):
        ticket = str(int(getattr(position, "ticket", 0) or 0))
        item = meta.setdefault(ticket, {})
        if str(item.get("leg", "")) != "tp2" or item.get("be_done"):
            continue
        side = "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell"
        entry = float(getattr(position, "price_open", 0.0) or 0.0)
        current = float(getattr(position, "price_current", 0.0) or 0.0)
        trigger_dist = float(item.get("be_trigger_dist", 0.0) or 0.0)
        if trigger_dist <= 0.0:
            continue
        reached = current >= entry + trigger_dist if side == "buy" else current <= entry - trigger_dist
        if not reached:
            continue
        new_sl = entry + float(cfg.btc_scalp_be_buffer_usd) if side == "buy" else entry - float(cfg.btc_scalp_be_buffer_usd)
        old_sl = float(getattr(position, "sl", 0.0) or 0.0)
        better = new_sl > old_sl if side == "buy" else old_sl <= 0 or new_sl < old_sl
        if not better:
            item["be_done"] = True
            changed = True
            continue
        result = modify_position(position, round(new_sl, 2), float(getattr(position, "tp", 0.0) or 0.0))
        retcode = getattr(result, "retcode", None)
        _append_jsonl(events_path, {"type": "protect_be", "ticket": int(ticket), "new_sl": round(new_sl, 2), "retcode": retcode})
        if retcode in {10008, 10009}:
            item["be_done"] = True
            changed = True
    return changed


def _record_closed(symbol: str, cfg, meta: dict, trades_path: Path, events_path: Path) -> bool:
    live = {str(int(getattr(position, "ticket", 0) or 0)) for position in positions_by_magic(symbol, int(cfg.btc_scalp_magic))}
    changed = False
    for ticket, item in list(meta.items()):
        if ticket in live or item.get("closed"):
            continue
        profit, gross_profit, costs, exit_price = _closed_position_summary(ticket, item)
        _append_trade(trades_path, {"ticket": ticket, "side": item.get("side", ""), "volume": item.get("volume", 0.0), "entry": item.get("entry", 0.0), "exit": exit_price, "profit": round(profit, 2), "gross_profit": round(gross_profit, 2), "costs": round(costs, 2), "reason": "mt5_position_closed", "strategy": "btc scalp"})
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
    _ensure_trades(trades_path)

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = _ensure_btc_symbol(cfg.btc_scalp_symbol)
    runtime = _read_json(runtime_path, {})
    meta = _read_json(meta_path, {})
    account = account_info()
    day_key = runtime.get("day_key") or trading_day_key()
    day_start_equity = float(runtime.get("day_start_equity", float(account.equity)))
    start_equity = float(runtime.get("start_equity", float(account.equity)))
    runtime.setdefault("start_equity", start_equity)

    try:
        while True:
            cfg = load_settings()
            now = datetime.now(UTC)
            if trading_day_key() != day_key:
                day_key = trading_day_key()
                day_start_equity = float(account_info().equity)
            account = account_info()
            equity = float(account.equity)
            daily_dd = max(0.0, ((day_start_equity - equity) / day_start_equity) * 100.0) if day_start_equity > 0 else 0.0
            total_dd = max(0.0, ((start_equity - equity) / start_equity) * 100.0) if start_equity > 0 else 0.0
            positions = positions_by_magic(symbol, int(cfg.btc_scalp_magic))
            spread = current_spread_points(symbol)

            if _protect_positions(symbol, cfg, meta, events_path) or _record_closed(symbol, cfg, meta, trades_path, events_path):
                _write_json(meta_path, meta)

            signal, diagnostics = _signal_from_market(symbol, cfg)
            can_trade = (
                bool(cfg.btc_scalp_enabled)
                and signal is not None
                and spread <= float(cfg.btc_scalp_max_spread_points)
                and daily_dd < float(cfg.btc_scalp_daily_dd_pct)
                and total_dd < float(cfg.btc_scalp_total_dd_pct)
                and len(positions) < int(cfg.btc_scalp_max_positions)
            )
            last_trade_epoch = float(runtime.get("last_trade_epoch", 0.0) or 0.0)
            if time.time() - last_trade_epoch < int(cfg.btc_scalp_cooldown_seconds):
                can_trade = False
                diagnostics["blocked"] = "cooldown"

            if can_trade and signal:
                total_lot = normalize_volume(symbol, float(cfg.btc_scalp_lot), float(cfg.min_lot), float(cfg.max_lot))
                leg_lot = normalize_volume(symbol, total_lot / 3.0, float(cfg.min_lot), float(cfg.max_lot))
                legs = [("tp1a", signal["tp1"]), ("tp1b", signal["tp1"]), ("tp2", signal["tp2"])]
                opened = []
                for leg, tp in legs:
                    result = send_market_order(symbol=symbol, side=signal["side"], volume=leg_lot, sl=signal["sl"], tp=tp, deviation=cfg.deviation, magic=int(cfg.btc_scalp_magic), comment=f"btc scalp:{leg}")
                    retcode = getattr(result, "retcode", None)
                    ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
                    opened.append({"leg": leg, "tp": tp, "retcode": retcode, "ticket": ticket})
                    _append_jsonl(events_path, {"type": "open_attempt", "signal": signal, "leg": leg, "volume": leg_lot, "tp": tp, "retcode": retcode})
                time.sleep(0.5)
                for position in positions_by_magic(symbol, int(cfg.btc_scalp_magic)):
                    ticket = str(int(getattr(position, "ticket", 0) or 0))
                    if ticket in meta and not meta[ticket].get("closed"):
                        continue
                    comment = str(getattr(position, "comment", "") or "")
                    meta[ticket] = {
                        "created_utc": datetime.now(UTC).isoformat(),
                        "strategy": "btc scalp",
                        "leg": "tp2" if "tp2" in comment else "tp1",
                        "side": "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell",
                        "volume": float(getattr(position, "volume", 0.0) or 0.0),
                        "entry": float(getattr(position, "price_open", 0.0) or 0.0),
                        "tp1": float(signal["tp1"]),
                        "tp2": float(signal["tp2"]),
                        "sl": float(signal["sl"]),
                        "be_trigger_dist": float(signal["be_trigger_dist"]),
                        "be_done": False,
                    }
                runtime["last_trade_epoch"] = time.time()
                runtime["last_signal"] = signal
                runtime["last_opened"] = opened
                _write_json(meta_path, meta)

            positions = positions_by_magic(symbol, int(cfg.btc_scalp_magic))
            open_profit = sum(float(getattr(position, "profit", 0.0) or 0.0) for position in positions)
            day_closed_profit = _daily_closed_profit(trades_path)
            runtime.update({"day_key": day_key, "day_start_equity": day_start_equity, "last_heartbeat_utc": now.isoformat()})
            _write_json(runtime_path, runtime)
            _write_json(
                status_path,
                {
                    "heartbeat_utc": now.isoformat(),
                    "enabled": bool(cfg.btc_scalp_enabled),
                    "symbol": symbol,
                    "magic": int(cfg.btc_scalp_magic),
                    "account": {"login": int(account.login), "server": str(account.server), "balance": float(account.balance), "equity": float(account.equity), "profit": float(account.profit)},
                    "positions_count": len(positions),
                    "open_profit": round(open_profit, 2),
                    "day_realized_profit": round(day_closed_profit, 2),
                    "total_lot": float(cfg.btc_scalp_lot),
                    "max_positions": int(cfg.btc_scalp_max_positions),
                    "max_spread_points": int(cfg.btc_scalp_max_spread_points),
                    "last_signal_side": signal.get("side") if signal else None,
                    "last_reason": diagnostics.get("reason") or diagnostics.get("blocked") or "ready",
                    "risk": {"spread_points": spread, "daily_dd_pct": round(daily_dd, 4), "total_dd_pct": round(total_dd, 4), "max_daily_dd_pct": float(cfg.btc_scalp_daily_dd_pct), "max_total_dd_pct": float(cfg.btc_scalp_total_dd_pct), "daily_closed_profit": round(day_closed_profit, 2)},
                    "settings": {"lot": float(cfg.btc_scalp_lot), "leg_lot": round(float(cfg.btc_scalp_lot) / 3.0, 4), "sl_atr_mult": float(cfg.btc_scalp_sl_atr_mult), "tp1_atr_mult": float(cfg.btc_scalp_tp1_atr_mult), "tp2_atr_mult": float(cfg.btc_scalp_tp2_atr_mult), "be_trigger_atr_mult": float(cfg.btc_scalp_be_trigger_atr_mult), "cooldown_seconds": int(cfg.btc_scalp_cooldown_seconds)},
                    "signal": signal,
                    "diagnostics": diagnostics,
                    "positions": [_status_position(position) for position in positions],
                },
            )
            time.sleep(float(cfg.btc_scalp_loop_seconds))
    finally:
        shutdown()
