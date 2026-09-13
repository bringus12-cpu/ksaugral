from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _parse_signal


CHANNEL = "Nas100freepip"
CHANNEL_ID = -1001473647097
OUT = Path("data_vantage/backtest_nas100freepip_40d.json")


@dataclass
class Trade:
    time: datetime
    message_id: int
    asset: str
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    status: str
    exit: float
    profit_001: float
    bars_to_exit: int
    text: str


def _tf(name: str) -> int:
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}[name.upper()]


def _rates(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, _tf("M1"), start, end)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _hit_tp(side: str, tp: float, high: float, low: float) -> bool:
    return high >= tp if side == "buy" else low <= tp


def _hit_sl(side: str, sl: float, high: float, low: float) -> bool:
    return low <= sl if side == "buy" else high >= sl


def _entry_touched(entry: float, high: float, low: float) -> bool:
    return low <= entry <= high


def _profit(side: str, entry: float, exit_price: float) -> float:
    return exit_price - entry if side == "buy" else entry - exit_price


def _valid(side: str, entry: float, sl: float, tp: float) -> bool:
    if min(entry, sl, tp) <= 0:
        return False
    if side == "buy":
        return sl < entry < tp
    return tp < entry < sl


def _simulate(signal: Any, symbol: str, rates: pd.DataFrame, start_idx: int, horizon_hours: int = 12) -> dict[str, Any]:
    if len(signal.tps) < 1:
        return {"status": "skipped", "reason": "no_tp"}
    entry = float(signal.entry or 0.0)
    if entry <= 0 and signal.entries:
        entry = float(signal.entries[0])
    if entry <= 0:
        entry = float(rates.iloc[start_idx]["close"])
    sl = float(signal.sl or 0.0)
    tp1 = float(signal.tps[0])
    tp2 = float(signal.tps[1] if len(signal.tps) > 1 else signal.tps[0])
    if not _valid(signal.side, entry, sl, tp1):
        return {"status": "skipped", "reason": "invalid_levels"}

    end_time = rates.iloc[start_idx]["time"] + pd.Timedelta(hours=horizon_hours)
    end_idx = min(int(rates["time"].searchsorted(end_time, side="right")), len(rates))
    trigger_idx = start_idx
    market = float(rates.iloc[start_idx]["close"])
    pending = signal.order_kind in {"limit", "stop"} or abs(entry - market) > max(2.0, abs(tp1 - entry) * 0.3)
    if pending:
        trigger_idx = -1
        expiry_time = rates.iloc[start_idx]["time"] + pd.Timedelta(minutes=60)
        expiry_idx = min(int(rates["time"].searchsorted(expiry_time, side="right")), len(rates))
        for idx in range(start_idx, expiry_idx):
            row = rates.iloc[idx]
            if _hit_tp(signal.side, tp1, float(row["high"]), float(row["low"])):
                return {"status": "expired", "reason": "tp1_before_entry"}
            if _entry_touched(entry, float(row["high"]), float(row["low"])):
                trigger_idx = idx
                break
        if trigger_idx < 0:
            return {"status": "expired", "reason": "not_triggered"}

    current_sl = sl
    tp1_seen = False
    for idx in range(trigger_idx, end_idx):
        row = rates.iloc[idx]
        high = float(row["high"])
        low = float(row["low"])
        if _hit_sl(signal.side, current_sl, high, low):
            status = "be" if abs(current_sl - entry) < 0.05 else "loss"
            return {"status": status, "entry": entry, "exit": current_sl, "bars": idx - trigger_idx, "tp1": tp1, "tp2": tp2}
        if _hit_tp(signal.side, tp2, high, low):
            return {"status": "win_tp2", "entry": entry, "exit": tp2, "bars": idx - trigger_idx, "tp1": tp1, "tp2": tp2}
        if _hit_tp(signal.side, tp1, high, low):
            tp1_seen = True
            current_sl = entry
        if tp1_seen and _hit_sl(signal.side, current_sl, high, low):
            return {"status": "be", "entry": entry, "exit": current_sl, "bars": idx - trigger_idx, "tp1": tp1, "tp2": tp2}
    close = float(rates.iloc[max(trigger_idx, end_idx - 1)]["close"])
    return {"status": "timeout", "entry": entry, "exit": close, "bars": max(0, end_idx - trigger_idx), "tp1": tp1, "tp2": tp2}


def _symbol_for(asset: str) -> str | None:
    if asset == "nas100":
        return "NAS100"
    if asset == "us30":
        return "DJ30"
    return None


async def main() -> None:
    load_dotenv(".env.vantage", override=True)
    cfg = load_settings()
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    end = datetime.now(UTC)
    start = end - timedelta(days=40)
    rates_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol in ["NAS100", "DJ30"]:
        try:
            selected = ensure_symbol(symbol)
        except Exception:
            continue
        frame = _rates(selected, start - timedelta(hours=12), end + timedelta(hours=12))
        if not frame.empty:
            rates_by_symbol[selected] = frame

    session_src = cfg.data_dir / cfg.telegram_session_name
    session_path = cfg.data_dir / "analysis_nas100freepip"
    src_file = Path(str(session_src) + ".session")
    dst_file = Path(str(session_path) + ".session")
    if src_file.exists():
        dst_file.write_bytes(src_file.read_bytes())

    client = TelegramClient(str(session_path), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    trades: list[Trade] = []
    parsed = 0
    by_asset_seen: dict[str, int] = defaultdict(int)
    skip_reasons: dict[str, int] = defaultdict(int)
    try:
        entity = await client.get_entity(CHANNEL)
        title = str(getattr(entity, "title", "") or CHANNEL)
        async for message in client.iter_messages(entity):
            msg_time = getattr(message, "date", None)
            if msg_time and msg_time < start:
                break
            text = str(getattr(message, "raw_text", "") or "")
            signal = _parse_signal(text, f"{CHANNEL_ID}:{message.id}", CHANNEL_ID, title, "", int(message.id or 0))
            if signal is None:
                continue
            parsed += 1
            by_asset_seen[signal.asset] += 1
            symbol_name = _symbol_for(signal.asset)
            if not symbol_name:
                skip_reasons[f"unsupported_asset:{signal.asset}"] += 1
                continue
            symbol = ensure_symbol(symbol_name)
            rates = rates_by_symbol.get(symbol)
            if rates is None or rates.empty:
                skip_reasons[f"no_rates:{symbol}"] += 1
                continue
            start_idx = int(rates["time"].searchsorted(pd.Timestamp(msg_time), side="left"))
            if start_idx >= len(rates):
                skip_reasons["after_rates"] += 1
                continue
            result = _simulate(signal, symbol, rates, start_idx)
            if result["status"] in {"skipped", "expired"}:
                skip_reasons[str(result.get("reason") or result["status"])] += 1
                continue
            profit_001 = _profit(signal.side, float(result["entry"]), float(result["exit"]))
            trades.append(
                Trade(
                    time=msg_time,
                    message_id=int(message.id or 0),
                    asset=signal.asset,
                    symbol=symbol,
                    side=signal.side,
                    entry=float(result["entry"]),
                    sl=float(signal.sl or 0.0),
                    tp1=float(result["tp1"]),
                    tp2=float(result["tp2"]),
                    status=str(result["status"]),
                    exit=float(result["exit"]),
                    profit_001=profit_001,
                    bars_to_exit=int(result["bars"]),
                    text=text[:500],
                )
            )
    finally:
        await client.disconnect()
        shutdown()

    by_asset: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "be": 0, "timeouts": 0, "profit_001": 0.0})
    for trade in trades:
        row = by_asset[trade.asset]
        row["n"] += 1
        row["profit_001"] += trade.profit_001
        if trade.status.startswith("win"):
            row["wins"] += 1
        elif trade.status == "loss":
            row["losses"] += 1
        elif trade.status == "be":
            row["be"] += 1
        else:
            row["timeouts"] += 1
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "channel": CHANNEL,
        "range_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "parsed_signals": parsed,
        "seen_assets": dict(by_asset_seen),
        "played_trades": len(trades),
        "skip_reasons": dict(skip_reasons),
        "by_asset": {
            key: {
                **value,
                "win_rate_wl_pct": round(value["wins"] / max(1, value["wins"] + value["losses"]) * 100, 2),
                "profit_at_001_lot": round(value["profit_001"], 2),
                "profit_at_015_lot": round(value["profit_001"] * 15.0, 2),
            }
            for key, value in by_asset.items()
        },
        "trades": [trade.__dict__ | {"time": trade.time.isoformat()} for trade in trades],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    asyncio.run(main())
