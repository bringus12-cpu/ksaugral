from __future__ import annotations

import csv
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import load_settings
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    close_position,
    connect,
    ensure_symbol,
    get_tick,
    mt5,
    positions_by_magic,
    send_market_order,
    shutdown,
)
from .risk import current_spread_points, normalize_volume, trading_day_key


STATUS_FILE = "xau_contr_scalp_status.json"
EVENTS_FILE = "xau_contr_scalp_events.jsonl"
TRADES_FILE = "xau_contr_scalp_trades.csv"
META_FILE = "xau_contr_scalp_meta.json"
RUNTIME_FILE = "xau_contr_scalp_runtime.json"
SOURCE_TRADES_FILE = "xau_scalp_trades.csv"

MODE_PRESETS = {
    "safe": {"factor": 0.50, "tp": 5.0, "sl": 5.0},
    "normal": {"factor": 0.75, "tp": 6.0, "sl": 6.0},
    "aggressive": {"factor": 1.00, "tp": 8.0, "sl": 6.0},
    "mega": {"factor": 1.50, "tp": 8.0, "sl": 6.0},
}


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
            fieldnames=[
                "closed_at_utc",
                "ticket",
                "source_ticket",
                "side",
                "volume",
                "entry",
                "exit",
                "profit",
                "gross_profit",
                "costs",
                "reason",
                "strategy",
            ],
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


def _daily_closed_profit(path: Path) -> float:
    today = datetime.now(UTC).date().isoformat()
    if not path.exists():
        return 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return sum(
                float(row.get("profit", 0.0) or 0.0)
                for row in csv.DictReader(handle)
                if str(row.get("closed_at_utc", "")).startswith(today)
            )
    except Exception:
        return 0.0


def _last_source_losses(path: Path, processed: set[str]) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return []
    losses: list[dict] = []
    for row in rows[-80:]:
        ticket = str(row.get("ticket", "") or "")
        if not ticket or ticket in processed:
            continue
        try:
            profit = float(row.get("profit", 0.0) or 0.0)
            volume = float(row.get("volume", 0.0) or 0.0)
            exit_price = float(row.get("exit", row.get("entry", 0.0)) or 0.0)
        except Exception:
            continue
        side = str(row.get("side", "") or "").strip().lower()
        if profit < 0.0 and side in {"buy", "sell"} and volume > 0.0 and exit_price > 0.0:
            losses.append({**row, "ticket": ticket, "profit": profit, "volume": volume, "exit": exit_price, "side": side})
    return losses


def _mode_settings(cfg) -> dict:
    mode = str(getattr(cfg, "xau_contr_scalp_mode", "normal") or "normal").lower()
    preset = MODE_PRESETS.get(mode, MODE_PRESETS["normal"])
    factor = float(getattr(cfg, "xau_contr_scalp_lot_factor", 0.0) or 0.0)
    tp = float(getattr(cfg, "xau_contr_scalp_tp_usd", 0.0) or 0.0)
    sl = float(getattr(cfg, "xau_contr_scalp_sl_usd", 0.0) or 0.0)
    return {
        "mode": mode if mode in MODE_PRESETS else "normal",
        "factor": factor if factor > 0 else preset["factor"],
        "tp": tp if tp > 0 else preset["tp"],
        "sl": sl if sl > 0 else preset["sl"],
    }


def _status_position(position) -> dict:
    side = "BUY" if int(getattr(position, "type", 0) or 0) == 0 else "SELL"
    return {
        "ticket": int(getattr(position, "ticket", 0) or 0),
        "symbol": str(getattr(position, "symbol", "")),
        "side": side,
        "volume": float(getattr(position, "volume", 0.0) or 0.0),
        "price_open": float(getattr(position, "price_open", 0.0) or 0.0),
        "current": float(getattr(position, "price_current", 0.0) or 0.0),
        "sl": float(getattr(position, "sl", 0.0) or 0.0),
        "tp": float(getattr(position, "tp", 0.0) or 0.0),
        "profit": float(getattr(position, "profit", 0.0) or 0.0),
        "comment": str(getattr(position, "comment", "") or ""),
        "time": int(getattr(position, "time", 0) or 0),
    }


def _record_closed(symbol: str, cfg, meta: dict, trades_path: Path, events_path: Path) -> bool:
    live = {str(int(getattr(position, "ticket", 0) or 0)) for position in positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic))}
    changed = False
    for ticket, item in list(meta.get("positions", {}).items()):
        if ticket in live or item.get("closed"):
            continue
        profit, gross_profit, costs, exit_price = _closed_position_summary(ticket, item)
        _append_trade(
            trades_path,
            {
                "ticket": ticket,
                "source_ticket": item.get("source_ticket", ""),
                "side": item.get("side", ""),
                "volume": item.get("volume", 0.0),
                "entry": item.get("entry", 0.0),
                "exit": exit_price,
                "profit": round(profit, 2),
                "gross_profit": round(gross_profit, 2),
                "costs": round(costs, 2),
                "reason": "mt5_position_closed",
                "strategy": "kontrscalper",
            },
        )
        item["closed"] = True
        item["closed_utc"] = datetime.now(UTC).isoformat()
        item["closed_profit"] = round(profit, 2)
        _append_jsonl(events_path, {"type": "closed", "ticket": ticket, "source_ticket": item.get("source_ticket"), "profit": round(profit, 2)})
        changed = True
    return changed


def _close_expired_positions(symbol: str, cfg, meta: dict, events_path: Path) -> bool:
    changed = False
    timeout = int(getattr(cfg, "xau_contr_scalp_timeout_minutes", 15) or 15)
    for position in positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic)):
        ticket = str(int(getattr(position, "ticket", 0) or 0))
        item = meta.get("positions", {}).get(ticket, {})
        try:
            created = datetime.fromisoformat(str(item.get("created_utc", ""))).astimezone(UTC)
        except Exception:
            created = datetime.now(UTC)
        if datetime.now(UTC) - created < timedelta(minutes=timeout):
            continue
        result = close_position(position, int(cfg.deviation))
        _append_jsonl(events_path, {"type": "timeout_close_attempt", "ticket": ticket, "retcode": getattr(result, "retcode", None), "timeout_minutes": timeout})
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
    source_trades_path = data_dir / SOURCE_TRADES_FILE
    _ensure_trades(trades_path)

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    symbol = ensure_symbol(cfg.symbol)
    runtime = _read_json(runtime_path, {})
    meta = _read_json(meta_path, {"processed_source_tickets": [], "positions": {}})
    meta.setdefault("processed_source_tickets", [])
    meta.setdefault("positions", {})
    account = account_info()
    day_key = runtime.get("day_key") or trading_day_key()
    day_start_equity = float(runtime.get("day_start_equity", float(account.equity)))
    start_equity = float(runtime.get("start_equity", float(account.equity)))
    runtime.setdefault("start_equity", start_equity)

    try:
        while True:
            cfg = load_settings()
            settings = _mode_settings(cfg)
            now = datetime.now(UTC)
            if trading_day_key() != day_key:
                day_key = trading_day_key()
                day_start_equity = float(account_info().equity)
            account = account_info()
            equity = float(account.equity)
            daily_dd = max(0.0, ((day_start_equity - equity) / day_start_equity) * 100.0) if day_start_equity > 0 else 0.0
            total_dd = max(0.0, ((start_equity - equity) / start_equity) * 100.0) if start_equity > 0 else 0.0
            positions = positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic))
            spread = current_spread_points(symbol)
            processed = set(str(item) for item in meta.get("processed_source_tickets", []))

            if _close_expired_positions(symbol, cfg, meta, events_path) or _record_closed(symbol, cfg, meta, trades_path, events_path):
                _write_json(meta_path, meta)

            can_trade = (
                bool(cfg.xau_contr_scalp_enabled)
                and spread <= float(cfg.xau_contr_scalp_max_spread_points)
                and daily_dd < float(cfg.xau_contr_scalp_daily_dd_pct)
                and total_dd < float(cfg.xau_contr_scalp_total_dd_pct)
                and len(positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic))) < int(cfg.xau_contr_scalp_max_positions)
            )
            reason = "ready" if can_trade else "blocked"
            losses = _last_source_losses(source_trades_path, processed)
            opened: list[dict] = []
            if can_trade and losses:
                for loss in losses:
                    if len(positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic))) >= int(cfg.xau_contr_scalp_max_positions):
                        reason = "max_positions"
                        break
                    source_side = str(loss["side"])
                    side = "sell" if source_side == "buy" else "buy"
                    tick = get_tick(symbol)
                    entry = float(tick.ask if side == "buy" else tick.bid)
                    tp_dist = float(settings["tp"])
                    sl_dist = float(settings["sl"])
                    tp = entry + tp_dist if side == "buy" else entry - tp_dist
                    sl = entry - sl_dist if side == "buy" else entry + sl_dist
                    volume = normalize_volume(
                        symbol,
                        float(loss["volume"]) * float(settings["factor"]),
                        float(cfg.min_lot),
                        float(cfg.max_lot),
                    )
                    result = send_market_order(
                        symbol=symbol,
                        side=side,
                        volume=volume,
                        sl=round(sl, 2),
                        tp=round(tp, 2),
                        deviation=int(cfg.deviation),
                        magic=int(cfg.xau_contr_scalp_magic),
                        comment="kontrscalper",
                    )
                    retcode = getattr(result, "retcode", None)
                    ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
                    processed.add(str(loss["ticket"]))
                    meta["processed_source_tickets"] = sorted(processed)
                    opened.append({"source_ticket": loss["ticket"], "ticket": ticket, "side": side, "volume": volume, "retcode": retcode})
                    _append_jsonl(
                        events_path,
                        {
                            "type": "open_attempt",
                            "source_ticket": loss["ticket"],
                            "source_side": source_side,
                            "source_profit": loss["profit"],
                            "side": side,
                            "volume": volume,
                            "entry": round(entry, 2),
                            "sl": round(sl, 2),
                            "tp": round(tp, 2),
                            "mode": settings["mode"],
                            "retcode": retcode,
                        },
                    )
                    time.sleep(0.3)
                    for position in positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic)):
                        pticket = str(int(getattr(position, "ticket", 0) or 0))
                        if pticket in meta["positions"] and not meta["positions"][pticket].get("closed"):
                            continue
                        if "kontrscalper" not in str(getattr(position, "comment", "") or "").lower():
                            continue
                        meta["positions"][pticket] = {
                            "created_utc": datetime.now(UTC).isoformat(),
                            "strategy": "kontrscalper",
                            "source_ticket": loss["ticket"],
                            "source_profit": loss["profit"],
                            "side": "buy" if int(getattr(position, "type", 0) or 0) == 0 else "sell",
                            "volume": float(getattr(position, "volume", 0.0) or 0.0),
                            "entry": float(getattr(position, "price_open", 0.0) or 0.0),
                            "sl": float(getattr(position, "sl", 0.0) or 0.0),
                            "tp": float(getattr(position, "tp", 0.0) or 0.0),
                        }
                    _write_json(meta_path, meta)
            elif losses:
                for loss in losses:
                    processed.add(str(loss["ticket"]))
                meta["processed_source_tickets"] = sorted(processed)
                _write_json(meta_path, meta)

            positions = positions_by_magic(symbol, int(cfg.xau_contr_scalp_magic))
            open_profit = sum(float(getattr(position, "profit", 0.0) or 0.0) for position in positions)
            day_closed_profit = _daily_closed_profit(trades_path)
            runtime.update({"day_key": day_key, "day_start_equity": day_start_equity, "last_heartbeat_utc": now.isoformat(), "last_opened": opened})
            _write_json(runtime_path, runtime)
            _write_json(
                status_path,
                {
                    "heartbeat_utc": now.isoformat(),
                    "enabled": bool(cfg.xau_contr_scalp_enabled),
                    "symbol": symbol,
                    "magic": int(cfg.xau_contr_scalp_magic),
                    "account": {
                        "login": int(account.login),
                        "server": str(account.server),
                        "balance": float(account.balance),
                        "equity": float(account.equity),
                        "profit": float(account.profit),
                    },
                    "positions_count": len(positions),
                    "open_profit": round(open_profit, 2),
                    "day_realized_profit": round(day_closed_profit, 2),
                    "mode": settings["mode"],
                    "lot_factor": float(settings["factor"]),
                    "tp_usd": float(settings["tp"]),
                    "sl_usd": float(settings["sl"]),
                    "timeout_minutes": int(cfg.xau_contr_scalp_timeout_minutes),
                    "max_positions": int(cfg.xau_contr_scalp_max_positions),
                    "max_spread_points": int(cfg.xau_contr_scalp_max_spread_points),
                    "last_reason": reason,
                    "processed_source_tickets": len(processed),
                    "risk": {
                        "spread_points": spread,
                        "daily_dd_pct": round(daily_dd, 4),
                        "total_dd_pct": round(total_dd, 4),
                        "max_daily_dd_pct": float(cfg.xau_contr_scalp_daily_dd_pct),
                        "max_total_dd_pct": float(cfg.xau_contr_scalp_total_dd_pct),
                        "daily_closed_profit": round(day_closed_profit, 2),
                    },
                    "positions": [_status_position(position) for position in positions],
                },
            )
            time.sleep(float(cfg.xau_contr_scalp_loop_seconds))
    finally:
        shutdown()

