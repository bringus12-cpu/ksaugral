from __future__ import annotations
import time
from datetime import UTC, datetime

from .config import load_settings
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    close_position,
    connect,
    ensure_symbol,
    get_rates_df,
    positions_by_magic,
    send_market_order,
    shutdown,
    timeframe_seconds,
)
from .risk import compute_order_volume, current_spread_points, manage_position, trading_day_key
from .state import BotStateStore
from .strategies import generate_signal


def run() -> None:
    cfg = load_settings()
    store = BotStateStore(cfg.data_dir)
    runtime = store.load_runtime()
    meta = store.load_meta()

    connect(
        Mt5Credentials(
            login=cfg.mt5_login,
            password=cfg.mt5_password,
            server=cfg.mt5_server,
            path=cfg.mt5_path,
        )
    )
    symbol = ensure_symbol(cfg.symbol)

    info = account_info()
    day_key = runtime.get("day_key") or trading_day_key()
    day_start_equity = float(runtime.get("day_start_equity", float(info.equity)))

    try:
        while True:
            now = datetime.now(UTC)
            if trading_day_key() != day_key:
                day_key = trading_day_key()
                day_start_equity = float(account_info().equity)

            runtime.update({"day_key": day_key, "day_start_equity": day_start_equity})
            store.save_runtime(runtime)

            account = account_info()
            spread_points = current_spread_points(symbol)
            entry_df = get_rates_df(symbol, cfg.entry_timeframe, cfg.history_bars)
            regime_df = get_rates_df(symbol, cfg.regime_timeframe, cfg.history_bars)
            signal, diagnostics = generate_signal(entry_df, regime_df, cfg)
            positions = positions_by_magic(symbol, cfg.magic)

            entry_tf_seconds = timeframe_seconds(cfg.entry_timeframe)
            last_entry_bar_ts = entry_df.iloc[-1]["time"].isoformat()
            last_trade_ts = runtime.get("last_trade_ts", "")
            open_volume = sum(float(position.volume) for position in positions)
            daily_dd_pct = 0.0
            if day_start_equity > 0:
                daily_dd_pct = max(0.0, ((day_start_equity - float(account.equity)) / day_start_equity) * 100.0)
            cooldown_ok = True
            if last_trade_ts:
                try:
                    cooldown_ok = (
                        datetime.fromisoformat(last_entry_bar_ts) - datetime.fromisoformat(last_trade_ts)
                    ).total_seconds() >= (cfg.cooldown_bars * entry_tf_seconds)
                except Exception:
                    cooldown_ok = last_trade_ts != last_entry_bar_ts

            for position in positions:
                position_meta = meta.get(str(position.ticket), {})
                action = manage_position(position, signal, cfg, position_meta)
                if action.get("partial_close"):
                    partial_volume = round(float(position.volume) * cfg.partial_close_pct, 2)
                    remaining = round(float(position.volume) - partial_volume, 2)
                    broker_min = max(cfg.min_lot, 0.01)
                    if partial_volume >= broker_min and remaining >= broker_min:
                        result = close_position(position, cfg.deviation, partial_volume)
                        action["partial_retcode"] = getattr(result, "retcode", None)
                        if getattr(result, "retcode", None) == 10009:
                            position_meta["partial_done"] = 1
                    else:
                        action["partial_skipped"] = "volume_too_small"
                if action.get("close_due_to_reversal"):
                    position_meta["close_reason"] = "reversal_exit"
                    result = close_position(position, cfg.deviation)
                    action["close_retcode"] = getattr(result, "retcode", None)
                store.append_decision({"type": "manage_position", "action": action})
                meta[str(position.ticket)] = position_meta

            positions = positions_by_magic(symbol, cfg.magic)
            tracked_tickets = set(meta.keys())
            live_tickets = {str(position.ticket) for position in positions}
            closed_tickets = tracked_tickets - live_tickets
            for ticket in closed_tickets:
                closed_meta = meta.pop(ticket, {})
                store.append_trade(
                    {
                        "ticket": ticket,
                        "symbol": symbol,
                        "side": closed_meta.get("side", ""),
                        "volume": closed_meta.get("volume", 0.0),
                        "entry_price": closed_meta.get("entry_price", 0.0),
                        "exit_price": closed_meta.get("last_price", 0.0),
                        "profit": closed_meta.get("last_profit", 0.0),
                        "reason": closed_meta.get("close_reason", "mt5_position_closed"),
                        "strategy": closed_meta.get("strategy", ""),
                    }
                )

            can_open = (
                signal is not None
                and spread_points <= cfg.max_spread_points
                and daily_dd_pct < cfg.max_daily_drawdown_pct
                and (cfg.max_open_positions <= 0 or len(positions) < cfg.max_open_positions)
                and open_volume < cfg.max_total_lot
                and cooldown_ok
            )
            if can_open:
                volume = compute_order_volume(symbol, signal.side, signal.entry, signal.sl, float(account.equity), cfg)
                result = send_market_order(
                    symbol=symbol,
                    side=signal.side,
                    volume=volume,
                    sl=signal.sl,
                    tp=signal.tp,
                    deviation=cfg.deviation,
                    magic=cfg.magic,
                    comment=f"xau_autobot:{signal.strategy}",
                )
                retcode = getattr(result, "retcode", None)
                store.append_decision(
                    {
                        "type": "open_attempt",
                        "signal": signal.as_dict(),
                        "volume": volume,
                        "retcode": retcode,
                    }
                )
                if retcode == 10009:
                    runtime["last_trade_ts"] = last_entry_bar_ts
                    runtime["pending_open_meta"] = {
                        "strategy": signal.strategy,
                        "side": signal.side,
                        "volume": volume,
                        "entry_price": signal.entry,
                        "atr": signal.atr,
                        "partial_done": 0,
                        "close_reason": "unknown",
                        "last_price": signal.entry,
                        "last_profit": 0.0,
                    }
                    store.save_runtime(runtime)

            positions = positions_by_magic(symbol, cfg.magic)
            for position in positions:
                ticket = str(position.ticket)
                position_meta = meta.setdefault(ticket, {})
                position_meta["strategy"] = position_meta.get("strategy", str(getattr(position, "comment", "")))
                position_meta["side"] = "buy" if int(position.type) == 0 else "sell"
                position_meta["volume"] = float(position.volume)
                position_meta["entry_price"] = float(position.price_open)
                position_meta["last_price"] = float(position.price_current)
                position_meta["last_profit"] = float(position.profit)
                if "atr" not in position_meta:
                    pending_meta = runtime.get("pending_open_meta") or {}
                    position_meta["atr"] = float(pending_meta.get("atr", signal.atr if signal else 0.0))
                    position_meta["strategy"] = pending_meta.get("strategy", position_meta["strategy"])
                    position_meta["side"] = pending_meta.get("side", position_meta["side"])
                    position_meta["volume"] = float(pending_meta.get("volume", position_meta["volume"]))
                    position_meta["partial_done"] = int(pending_meta.get("partial_done", 0))
                    runtime["pending_open_meta"] = {}

            store.save_meta(meta)
            store.save_runtime(runtime)
            status = {
                "heartbeat_utc": now.isoformat(),
                "symbol": symbol,
                "account": {
                    "login": int(account.login),
                    "server": str(account.server),
                    "balance": float(account.balance),
                    "equity": float(account.equity),
                    "margin_free": float(account.margin_free),
                    "profit": float(account.profit),
                },
                "risk": {
                    "day_start_equity": day_start_equity,
                    "daily_drawdown_pct": daily_dd_pct,
                    "spread_points": spread_points,
                },
                "signal": signal.as_dict() if signal else None,
                "diagnostics": diagnostics,
                "positions": [store.serialize_position(position) for position in positions],
                "runtime": {
                    "magic": cfg.magic,
                    "entry_timeframe": cfg.entry_timeframe,
                    "regime_timeframe": cfg.regime_timeframe,
                    "last_trade_ts": runtime.get("last_trade_ts"),
                    "legacy_env_path": str(cfg.legacy_env_path),
                },
            }
            store.write_status(status)
            time.sleep(cfg.loop_seconds)
    finally:
        shutdown()
