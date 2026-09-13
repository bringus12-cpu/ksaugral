from __future__ import annotations

from datetime import UTC, datetime

from .config import Settings
from .mt5_gateway import calc_loss_per_lot, get_tick, modify_position, mt5, symbol_info


def normalize_volume(symbol: str, volume: float, min_lot: float, max_lot: float) -> float:
    info = symbol_info(symbol)
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    broker_min = float(getattr(info, "volume_min", 0.01) or 0.01)
    broker_max = float(getattr(info, "volume_max", max_lot) or max_lot)
    effective_min = max(min_lot, broker_min)
    effective_max = min(max_lot, broker_max)
    steps = round(volume / step)
    normalized = round(steps * step, 2)
    normalized = max(effective_min, normalized)
    normalized = min(effective_max, normalized)
    return round(normalized, 2)


def compute_order_volume(symbol: str, side: str, entry: float, sl: float, equity: float, cfg: Settings) -> float:
    risk_amount = max(0.0, equity * (cfg.risk_per_trade_pct / 100.0))
    loss_per_lot = calc_loss_per_lot(symbol, side, entry, sl)
    if loss_per_lot <= 0:
        return cfg.min_lot
    raw_volume = risk_amount / loss_per_lot
    return normalize_volume(symbol, raw_volume, cfg.min_lot, cfg.max_lot)


def current_spread_points(symbol: str) -> float:
    tick = get_tick(symbol)
    info = symbol_info(symbol)
    point = float(getattr(info, "point", 0.01) or 0.01)
    return abs(float(tick.ask) - float(tick.bid)) / point


def current_r_multiple(position) -> float:
    open_price = float(position.price_open)
    sl = float(position.sl or 0.0)
    if sl <= 0:
        return 0.0
    risk = abs(open_price - sl)
    if risk <= 0:
        return 0.0
    tick = get_tick(position.symbol)
    current_price = float(tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask)
    pnl_distance = current_price - open_price if position.type == mt5.POSITION_TYPE_BUY else open_price - current_price
    return pnl_distance / risk


def manage_position(position, signal, cfg: Settings, meta: dict) -> dict:
    atr = float(meta.get("atr", 0.0) or 0.0)
    r_multiple = current_r_multiple(position)
    action = {"ticket": int(position.ticket), "events": []}
    new_sl = float(position.sl or 0.0)
    tp = float(position.tp or 0.0)
    open_price = float(position.price_open)
    side = "buy" if position.type == mt5.POSITION_TYPE_BUY else "sell"

    if r_multiple >= cfg.breakeven_at_r:
        be_sl = open_price + (0.05 * atr if side == "buy" else -0.05 * atr)
        if side == "buy" and be_sl > new_sl:
            new_sl = be_sl
            action["events"].append("breakeven")
        if side == "sell" and (new_sl == 0.0 or be_sl < new_sl):
            new_sl = be_sl
            action["events"].append("breakeven")

    if atr > 0 and r_multiple >= cfg.trail_start_r:
        tick = get_tick(position.symbol)
        if side == "buy":
            trail_sl = float(tick.bid) - atr * cfg.trail_atr_mult
            if trail_sl > new_sl:
                new_sl = trail_sl
                action["events"].append("trail")
        else:
            trail_sl = float(tick.ask) + atr * cfg.trail_atr_mult
            if new_sl == 0.0 or trail_sl < new_sl:
                new_sl = trail_sl
                action["events"].append("trail")

    if action["events"] and abs(new_sl - float(position.sl or 0.0)) > 1e-9:
        result = modify_position(position, new_sl, tp)
        action["modify_retcode"] = getattr(result, "retcode", None)

    if signal is not None and signal.side != side and signal.score >= cfg.reversal_exit_score:
        action["close_due_to_reversal"] = True

    if int(meta.get("partial_done", 0)) == 0 and r_multiple >= cfg.partial_close_at_r:
        action["partial_close"] = True

    return action


def trading_day_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")
