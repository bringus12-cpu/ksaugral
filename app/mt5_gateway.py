from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import time
import json
import logging
from pathlib import Path
from datetime import UTC
from typing import Optional

import pandas as pd

try:
    import MetaTrader5 as mt5
except Exception as exc:
    mt5 = None
    _import_error = exc
else:
    _import_error = None


TIMEFRAME_MAP = {
    "M1": getattr(mt5, "TIMEFRAME_M1", None),
    "M5": getattr(mt5, "TIMEFRAME_M5", None),
    "M15": getattr(mt5, "TIMEFRAME_M15", None),
    "M30": getattr(mt5, "TIMEFRAME_M30", None),
    "H1": getattr(mt5, "TIMEFRAME_H1", None),
    "H4": getattr(mt5, "TIMEFRAME_H4", None),
    "D1": getattr(mt5, "TIMEFRAME_D1", None),
}

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


_last_credentials: Optional[Mt5Credentials] = None
_last_reconnect_attempt = 0.0
_RECONNECT_COOLDOWN_SECONDS = 5.0


@dataclass
class Mt5Credentials:
    login: int
    password: str
    server: str
    path: str


def ensure_mt5_available() -> None:
    if mt5 is None:
        raise RuntimeError(f"MetaTrader5 import failed: {_import_error}")


def connect(credentials: Mt5Credentials) -> None:
    global _last_credentials
    ensure_mt5_available()
    init_kwargs = {
        "login": credentials.login,
        "server": credentials.server,
        "timeout": 20000,
    }
    if credentials.password:
        init_kwargs["password"] = credentials.password
    if credentials.path:
        init_kwargs["portable"] = str(os.getenv("MT5_PORTABLE", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
        ok = mt5.initialize(path=credentials.path, **init_kwargs)
    else:
        ok = mt5.initialize(**init_kwargs)
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    if credentials.login and credentials.server:
        last_login_error = None
        for _ in range(3):
            login_kwargs = {
                "login": credentials.login,
                "server": credentials.server,
                "timeout": 20000,
            }
            if credentials.password:
                login_kwargs["password"] = credentials.password
            logged_in = mt5.login(**login_kwargs)
            if not logged_in:
                last_login_error = mt5.last_error()
                time.sleep(0.75)
                continue
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                info = mt5.account_info()
                if info is not None and int(getattr(info, "login", 0) or 0) == int(credentials.login):
                    _last_credentials = credentials
                    return
                time.sleep(0.5)
        info = mt5.account_info()
        raise RuntimeError(
            f"MT5 login verification failed: expected {credentials.login}, "
            f"got {getattr(info, 'login', None)}, last_error={last_login_error or mt5.last_error()}"
        )
    _last_credentials = credentials


def reconnect() -> bool:
    global _last_reconnect_attempt
    credentials = _last_credentials
    if credentials is None:
        return False
    now = time.monotonic()
    if now - _last_reconnect_attempt < _RECONNECT_COOLDOWN_SECONDS:
        return False
    _last_reconnect_attempt = now
    try:
        mt5.shutdown()
        connect(credentials)
    except Exception:
        return False
    return True

def shutdown() -> None:
    if mt5 is not None:
        mt5.shutdown()


def resolve_timeframe(name: str) -> int:
    timeframe = TIMEFRAME_MAP.get(name.upper())
    if timeframe is None:
        raise RuntimeError(f"Unsupported timeframe: {name}")
    return timeframe


def timeframe_seconds(name: str) -> int:
    return TIMEFRAME_SECONDS.get(name.upper(), 300)


def _symbol_trade_enabled(info) -> bool:
    disabled = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
    trade_mode = getattr(info, "trade_mode", None)
    return trade_mode is None or int(trade_mode) != int(disabled)


def _symbol_score(name: str, preferred: str, info) -> int:
    upper = name.upper()
    pref = preferred.upper()
    score = 0
    if upper == pref:
        score += 1000
    if upper.startswith(pref):
        score += 700
    gold_preferred = "XAU" in pref or "GOLD" in pref
    if gold_preferred and "XAUUSD" in upper:
        score += 500
    if gold_preferred and "XAU" in upper and "USD" in upper:
        score += 250
    if _symbol_trade_enabled(info):
        score += 200
    else:
        score -= 1000
    if getattr(info, "visible", False):
        score += 20
    if upper.endswith("+"):
        score += 10
    if ".CRP" in upper:
        score -= 100
    return score


def find_symbol(preferred: str) -> str:
    symbols = mt5.symbols_get() or []
    pref = preferred.upper()
    gold_preferred = "XAU" in pref or "GOLD" in pref
    candidates = []

    exact = mt5.symbol_info(preferred)
    if exact is not None:
        candidates.append((preferred, exact))

    for symbol in symbols:
        name = symbol.name
        upper = name.upper()
        if upper == pref or upper.startswith(pref) or (gold_preferred and "XAU" in upper and "USD" in upper):
            info = mt5.symbol_info(name)
            if info is not None:
                candidates.append((name, info))

    if not candidates:
        raise RuntimeError(f"Could not resolve symbol for {preferred}")

    unique: dict[str, object] = {}
    for name, info in candidates:
        unique.setdefault(name, info)
    ranked = sorted(unique.items(), key=lambda item: _symbol_score(item[0], preferred, item[1]), reverse=True)
    return ranked[0][0]


def ensure_symbol(symbol: str) -> str:
    resolved = find_symbol(symbol)
    info = mt5.symbol_info(resolved)
    if info is None:
        raise RuntimeError(f"Symbol not found in MT5: {resolved}")
    if not info.visible and not mt5.symbol_select(resolved, True):
        raise RuntimeError(f"Unable to select symbol {resolved}")
    return resolved


def symbol_info(symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_info failed for {symbol}")
    return info


def get_tick(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None and reconnect():
        info = mt5.symbol_info(symbol)
        if info is not None and (getattr(info, "visible", False) or mt5.symbol_select(symbol, True)):
            tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"symbol_info_tick failed for {symbol}: {mt5.last_error()}")
    return tick


def get_rates_df(symbol: str, timeframe_name: str, bars: int) -> pd.DataFrame:
    timeframe = resolve_timeframe(timeframe_name)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if (rates is None or len(rates) == 0) and reconnect():
        info = mt5.symbol_info(symbol)
        if info is not None and (getattr(info, "visible", False) or mt5.symbol_select(symbol, True)):
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No rates for {symbol} {timeframe_name}: {mt5.last_error()}")
    frame = pd.DataFrame(rates)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame


def account_info():
    info = mt5.account_info()
    if info is None and reconnect():
        info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"account_info failed: {mt5.last_error()}")
    return info


def positions_by_magic(symbol: str, magic: int) -> list:
    positions = mt5.positions_get(symbol=symbol) or []
    return [position for position in positions if int(getattr(position, "magic", 0)) == int(magic)]


def orders_by_magic(symbol: str, magic: int) -> list:
    orders = mt5.orders_get(symbol=symbol) or []
    return [order for order in orders if int(getattr(order, "magic", 0)) == int(magic)]


def _order_type(side: str) -> int:
    return mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL


def _pending_order_type(side: str, order_kind: str) -> int:
    if side == "buy" and order_kind == "limit":
        return mt5.ORDER_TYPE_BUY_LIMIT
    if side == "sell" and order_kind == "limit":
        return mt5.ORDER_TYPE_SELL_LIMIT
    if side == "buy" and order_kind == "stop":
        return mt5.ORDER_TYPE_BUY_STOP
    if side == "sell" and order_kind == "stop":
        return mt5.ORDER_TYPE_SELL_STOP
    raise ValueError(f"Unsupported pending order: {side} {order_kind}")


def _filling_modes(symbol: str) -> list[int]:
    info = mt5.symbol_info(symbol)
    candidates = [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    preferred = getattr(info, "filling_mode", None) if info is not None else None
    ordered: list[int] = []
    if preferred in candidates:
        ordered.append(preferred)
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _send_with_fallback(request: dict):
    last = None
    for filling in _filling_modes(request["symbol"]):
        payload = dict(request)
        payload["type_filling"] = filling
        result = None
        for attempt in range(5):
            result = _send_audited(payload)
            if result is not None:
                break
            if attempt < 4:
                time.sleep(0.10 * (attempt + 1))
        last = result
        if result is None:
            continue
        if getattr(result, "retcode", None) != 10030:
            return result
    return last


def trading_status() -> dict:
    """Describe terminal readiness separately from process liveness."""
    try:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        checks = {
            "connected": bool(terminal and terminal.connected),
            "algo_enabled": bool(terminal and terminal.trade_allowed),
            "python_api_enabled": bool(terminal and not terminal.tradeapi_disabled),
            "account_trade_allowed": bool(account and account.trade_allowed),
            "account_expert_allowed": bool(account and account.trade_expert),
        }
        return {**checks, "ready": all(checks.values()),
                "blocked_reasons": [key for key, value in checks.items() if not value],
                "account_login": getattr(account, "login", None)}
    except Exception as exc:
        return {"ready": False, "blocked_reasons": ["status_unavailable"], "error": type(exc).__name__}


def _send_audited(request: dict):
    started = time.monotonic()
    quote = {}
    try:
        tick = mt5.symbol_info_tick(request.get("symbol", ""))
        if tick:
            quote = {key: getattr(tick, key, None) for key in ("bid", "ask", "time_msc")}
    except Exception:
        pass
    result = mt5.order_send(request)
    try:
        # One journal per process avoids concurrent writes from different modules.
        folder = Path(os.getenv("DATA_DIR", "data")) / "execution_journal"
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp_utc": datetime.now(UTC).isoformat(), "pid": os.getpid(),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "quote": quote,
            "request": {key: request[key] for key in ("action", "symbol", "type", "volume", "price", "sl", "tp", "magic", "comment", "position", "order") if key in request},
            "result": {key: getattr(result, key, None) for key in ("retcode", "order", "deal", "price", "volume", "comment")},
            "unknown_outcome": result is None,
        }
        with (folder / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        logging.getLogger(__name__).warning("Could not write execution journal", exc_info=True)
    return result


def send_market_order(
    symbol: str,
    side: str,
    volume: float,
    sl: float,
    tp: float,
    deviation: int,
    magic: int,
    comment: str,
):
    tick = get_tick(symbol)
    price = tick.ask if side == "buy" else tick.bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": _order_type(side),
        "price": float(price),
        "sl": float(sl),
        "tp": float(tp),
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    return _send_with_fallback(request)


def send_pending_order(
    symbol: str,
    side: str,
    order_kind: str,
    volume: float,
    price: float,
    sl: float,
    tp: float,
    deviation: int,
    magic: int,
    comment: str,
):
    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": float(volume),
        "type": _pending_order_type(side, order_kind),
        "price": float(price),
        "sl": float(sl),
        "tp": float(tp),
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    return _send_with_fallback(request)


def close_position(position, deviation: int, volume: Optional[float] = None):
    close_side = "sell" if position.type == mt5.POSITION_TYPE_BUY else "buy"
    tick = get_tick(position.symbol)
    price = tick.bid if close_side == "sell" else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": float(position.volume if volume is None else volume),
        "type": _order_type(close_side),
        "position": int(position.ticket),
        "price": float(price),
        "deviation": int(deviation),
        "magic": int(position.magic),
        "comment": "xau_autobot_close",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    return _send_with_fallback(request)


def modify_position(position, sl: Optional[float], tp: Optional[float]):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": int(position.ticket),
        "sl": float(sl if sl is not None else 0.0),
        "tp": float(tp if tp is not None else 0.0),
        "magic": int(position.magic),
    }
    return _send_audited(request)


def modify_order(order, price: Optional[float], sl: Optional[float], tp: Optional[float], magic: Optional[int] = None):
    request = {
        "action": mt5.TRADE_ACTION_MODIFY,
        "order": int(getattr(order, "ticket", order)),
        "symbol": str(getattr(order, "symbol", "")),
        "price": float(price if price is not None else getattr(order, "price_open", 0.0)),
        "sl": float(sl if sl is not None else 0.0),
        "tp": float(tp if tp is not None else 0.0),
    }
    if magic is not None:
        request["magic"] = int(magic)
    return _send_audited(request)


def remove_order(order, magic: Optional[int] = None):
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": int(getattr(order, "ticket", order)),
    }
    if magic is not None:
        request["magic"] = int(magic)
    return _send_audited(request)


def calc_loss_per_lot(symbol: str, side: str, entry: float, stop: float) -> float:
    order_type = _order_type(side)
    profit = mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    if profit is None:
        info = symbol_info(symbol)
        tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
        if tick_value > 0 and tick_size > 0:
            return abs((entry - stop) / tick_size) * tick_value
        return abs(entry - stop)
    return abs(float(profit))
