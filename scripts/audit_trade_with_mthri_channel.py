from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown


@dataclass(frozen=True)
class Signal:
    message_id: int
    time: datetime
    side: str
    entries: tuple[float, float, float]
    tps: tuple[float, ...]
    sl: float | None
    sl_source: str


SUMMARY_MARKERS = (
    "tp all hit",
    "take profits",
    "amazing shot",
    "آپدیت",
    "با موفقیت زده شد",
)
NUMBER = r"(\d{3,5}(?:\.\d+)?)"
SUPERSCRIPTS = str.maketrans("¹²³⁴⁵⁶⁷⁸⁹⁰", "1234567890")


def _normalize(text: str) -> str:
    return text.translate(SUPERSCRIPTS).replace("\u00a0", " ")


def _side(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\bxauusd\s+buy\b", lowered) or "سیگنال خرید طلا" in text or "صفقة شراء" in text:
        return "buy"
    if re.search(r"\bxauusd\s+sell\b", lowered) or "سیگنال فروش طلا" in text or "صفقة بيع" in text:
        return "sell"
    return None


def _range(text: str) -> tuple[float, float] | None:
    patterns = (
        rf"entry\s*point\s*[:\-]?\s*{NUMBER}\s*[_/\-]\s*{NUMBER}",
        rf"(?:خرید|فروش)\s*طلا\s*[:.]\s*{NUMBER}\s*[/_\-]\s*{NUMBER}",
        rf"نقطة\s*الدخول\s*[:\-]?\s*{NUMBER}\s*[/_\-]\s*{NUMBER}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None


def _targets(text: str) -> tuple[float, ...]:
    found: list[tuple[int, float]] = []
    for match in re.finditer(rf"\bTP\s*(\d+)[^\d\n]{{0,12}}{NUMBER}", text, re.I):
        found.append((int(match.group(1)), float(match.group(2))))
    for match in re.finditer(rf"هدف\s*(\d+)\s*[:：.]\s*{NUMBER}", text):
        found.append((int(match.group(1)), float(match.group(2))))
    unique: dict[int, float] = {}
    for index, value in found:
        unique.setdefault(index, value)
    return tuple(value for _, value in sorted(unique.items()))


def _stop(text: str) -> float | None:
    patterns = (
        rf"\bSL\b\s*[:.]?\s*{NUMBER}",
        rf"حد\s*ضرر\s*\(?(?:SL)?\)?\s*[:.]?\s*{NUMBER}",
        rf"وقف\s*الخسارة\s*\(?(?:SL)?\)?\s*[:.]?\s*{NUMBER}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    return None


def parse_signal(row: dict[str, Any]) -> tuple[Signal | None, str]:
    text = _normalize(str(row.get("text", "")))
    lowered = text.lower()
    if any(marker in lowered or marker in text for marker in SUMMARY_MARKERS):
        return None, "result_or_promotion"
    side = _side(text)
    if not side:
        return None, "not_signal"
    raw_range = _range(text)
    tps = _targets(text)
    if not raw_range or not tps:
        return None, "missing_entry_or_tp"
    low, high = sorted(raw_range)
    entries = (low, round((low + high) / 2.0, 3), high)
    sl = _stop(text)
    if sl is not None:
        valid = sl < low if side == "buy" else sl > high
        if not valid:
            return None, "invalid_sl_geometry"
    valid_tps = all(tp > high for tp in tps) if side == "buy" else all(tp < low for tp in tps)
    if not valid_tps:
        return None, "invalid_tp_geometry"
    stamp = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00")).astimezone(UTC)
    return Signal(int(row["message_id"]), stamp, side, entries, tps, sl, "provider" if sl else "missing"), "valid"


def _market_distance(frame: pd.DataFrame, signal: Signal) -> tuple[float | None, float | None]:
    idx = int(frame.time.searchsorted(pd.Timestamp(signal.time), side="left"))
    if idx >= len(frame):
        return None, None
    price = float(frame.iloc[idx].open)
    low, high = min(signal.entries), max(signal.entries)
    distance = low - price if price < low else price - high if price > high else 0.0
    return price, round(distance, 3)


def _touch(entry: float, bar: pd.Series, side: str, point: float) -> bool:
    spread = float(bar.get("spread", 0.0) or 0.0) * point
    if side == "buy":
        return float(bar.low) + spread <= entry <= float(bar.high) + spread
    return float(bar.low) <= entry <= float(bar.high)


def _profit(symbol: str, side: str, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, 0.01, entry, exit_price) or 0.0) - 0.06


def _simulate_leg(
    frame: pd.DataFrame,
    symbol: str,
    signal: Signal,
    entry: float,
    sl: float,
    expiry_minutes: int,
    target_index: int,
    be_after_tp1: bool,
    point: float,
) -> dict[str, Any]:
    start_idx = int(frame.time.searchsorted(pd.Timestamp(signal.time), side="left"))
    expiry_time = signal.time + timedelta(minutes=expiry_minutes)
    expiry_idx = min(len(frame), int(frame.time.searchsorted(pd.Timestamp(expiry_time), side="right")))
    trigger_idx = next((idx for idx in range(start_idx, expiry_idx) if _touch(entry, frame.iloc[idx], signal.side, point)), -1)
    if trigger_idx < 0:
        return {"status": "expired", "pnl": 0.0}
    target = signal.tps[min(target_index, len(signal.tps)) - 1]
    tp1 = signal.tps[0]
    current_sl = sl
    end_idx = min(len(frame), int(frame.time.searchsorted(pd.Timestamp(signal.time + timedelta(hours=24)), side="right")))
    for idx in range(trigger_idx, end_idx):
        high, low = float(frame.iloc[idx].high), float(frame.iloc[idx].low)
        sl_hit = low <= current_sl if signal.side == "buy" else high >= current_sl
        target_hit = high >= target if signal.side == "buy" else low <= target
        tp1_hit = high >= tp1 if signal.side == "buy" else low <= tp1
        # Conservative ordering when a one-minute candle touches both levels.
        if sl_hit:
            return {"status": "be" if current_sl == entry else "sl", "pnl": _profit(symbol, signal.side, entry, current_sl)}
        if target_hit:
            return {"status": f"tp{min(target_index, len(signal.tps))}", "pnl": _profit(symbol, signal.side, entry, target)}
        if be_after_tp1 and target_index > 1 and tp1_hit:
            current_sl = entry
    close = float(frame.iloc[max(trigger_idx, end_idx - 1)].close)
    return {"status": "timeout", "pnl": _profit(symbol, signal.side, entry, close)}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [float(row["pnl"]) for row in rows]
    wins = sum(value > 0 for value in pnl)
    losses = sum(value < 0 for value in pnl)
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = abs(sum(value for value in pnl if value < 0))
    equity = peak = max_dd = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "signals": len(rows),
        "triggered_signals": sum(row["triggered_legs"] > 0 for row in rows),
        "triggered_legs": sum(row["triggered_legs"] for row in rows),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100 * wins / max(1, wins + losses), 2),
        "pnl_usd_001_per_leg": round(sum(pnl), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_closed_dd_usd": round(max_dd, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--messages", default="data_vantage/trade_with_mthri_90sessions_messages.jsonl")
    parser.add_argument("--sessions", type=int, default=90)
    parser.add_argument("--output", default="data_vantage/trade_with_mthri_90sessions_dedicated_audit.json")
    parser.add_argument("--max-market-distance", type=float, default=30.0)
    args = parser.parse_args()

    load_dotenv(ROOT / args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        daily = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, max(120, args.sessions + 20))
        dates = sorted({datetime.fromtimestamp(int(row["time"]), UTC).date() for row in daily})[-args.sessions:]
        since = datetime.combine(dates[0], datetime.min.time(), tzinfo=UTC)
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, since - timedelta(hours=1), datetime.now(UTC))
        frame = pd.DataFrame(raw)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        point = float(mt5.symbol_info(symbol).point or 0.01)
        rows = [json.loads(line) for line in (ROOT / args.messages).read_text(encoding="utf-8").splitlines() if line.strip()]

        parsed: list[Signal] = []
        rejections: dict[str, int] = {}
        for row in rows:
            signal, reason = parse_signal(row)
            rejections[reason] = rejections.get(reason, 0) + 1
            if signal and signal.time >= since:
                parsed.append(signal)
        parsed.sort(key=lambda item: item.time)

        classified = []
        plausible: list[Signal] = []
        for signal in parsed:
            market_price, distance = _market_distance(frame, signal)
            accepted = distance is not None and distance <= args.max_market_distance
            classified.append({
                "message_id": signal.message_id,
                "time_utc": signal.time.isoformat(),
                "side": signal.side,
                "entries": signal.entries,
                "tps": signal.tps,
                "provider_sl": signal.sl,
                "market_price": market_price,
                "market_distance": distance,
                "plausible": accepted,
            })
            if accepted:
                plausible.append(signal)

        variant_specs: dict[str, tuple[str, float | None, int, int, bool]] = {
            "provider_sl_tp1_expiry15": ("provider", None, 15, 1, False),
            "provider_sl_tp1_expiry60": ("provider", None, 60, 1, False),
            "inferred_sl7_tp1_expiry15": ("all", 7.0, 15, 1, False),
            "inferred_sl10_tp1_expiry15": ("all", 10.0, 15, 1, False),
            "inferred_sl13_tp1_expiry15": ("all", 13.0, 15, 1, False),
            "inferred_sl10_tp1_expiry60": ("all", 10.0, 60, 1, False),
            "inferred_sl10_tp2_be_expiry15": ("all", 10.0, 15, 2, True),
            "inferred_sl10_tp2_be_expiry60": ("all", 10.0, 60, 2, True),
        }
        summaries: dict[str, Any] = {}
        details: dict[str, Any] = {}
        for name, (scope, buffer, expiry, target, use_be) in variant_specs.items():
            outcomes = []
            for signal in plausible:
                if scope == "provider" and signal.sl is None:
                    continue
                sl = signal.sl
                if sl is None:
                    sl = min(signal.entries) - float(buffer) if signal.side == "buy" else max(signal.entries) + float(buffer)
                legs = [_simulate_leg(frame, symbol, signal, entry, sl, expiry, target, use_be, point) for entry in signal.entries]
                triggered = [leg for leg in legs if leg["status"] != "expired"]
                outcomes.append({
                    "message_id": signal.message_id,
                    "time_utc": signal.time.isoformat(),
                    "sl_source": "provider" if signal.sl is not None else f"inferred_{buffer:g}_usd",
                    "triggered_legs": len(triggered),
                    "statuses": [leg["status"] for leg in legs],
                    "pnl": round(sum(float(leg["pnl"]) for leg in legs), 2),
                })
            summaries[name] = _summary(outcomes)
            details[name] = outcomes

        exact = next((row for row in classified if row["message_id"] == 2306), None)
        report = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "channel": {"username": "Trade_With_MThri", "url": "https://t.me/Trade_With_MThri/2306"},
            "range": {"start": since.isoformat(), "end": datetime.now(UTC).isoformat(), "sessions": args.sessions},
            "messages": len(rows),
            "parsed_signal_posts": len(parsed),
            "provider_sl_posts": sum(signal.sl is not None for signal in parsed),
            "missing_sl_posts": sum(signal.sl is None for signal in parsed),
            "plausible_signals": len(plausible),
            "stale_or_outlier_signals": sum(not row["plausible"] for row in classified),
            "rejections": rejections,
            "message_2306": exact,
            "assumptions": {
                "lot_per_entry": 0.01,
                "entries": "low/mid/high of published range",
                "market_plausibility_max_distance_usd": args.max_market_distance,
                "commission_per_leg_usd": 0.06,
                "same_m1_bar_ordering": "SL first",
                "inferred_sl": "Only when the provider hid SL; buffer beyond the far edge. Synthetic, not provider-authored.",
            },
            "variants": summaries,
            "classified_signals": classified,
            "details": details,
            "limitations": [
                "Telegram exposes final edited text, not the full edit history.",
                "M1 candles cannot determine tick order when SL and TP occur in the same minute.",
                "Promotional TP-hit posts are excluded and never treated as execution evidence.",
                "Signals without a numeric provider SL cannot be safely copied live without an explicit fallback policy.",
            ],
        }
        output = ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("range", "messages", "parsed_signal_posts", "provider_sl_posts", "missing_sl_posts", "plausible_signals", "stale_or_outlier_signals", "rejections", "message_2306", "variants")}, ensure_ascii=False, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
