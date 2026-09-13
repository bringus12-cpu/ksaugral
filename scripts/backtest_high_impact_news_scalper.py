from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown


ET = ZoneInfo("America/New_York")
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_CACHE_PATH = Path(__file__).resolve().parent.parent / "data_vantage" / "bls_release_calendar.ics"


@dataclass(frozen=True)
class Event:
    name: str
    category: str
    utc: datetime


def _event(name: str, category: str, date: str, time_et: str) -> Event:
    local = datetime.fromisoformat(f"{date}T{time_et}:00").replace(tzinfo=ET)
    return Event(name, category, local.astimezone(UTC))


def _download_bls_events(start: datetime, end: datetime) -> list[Event]:
    request = Request(
        BLS_ICS_URL,
        headers={
            "User-Agent": "Mozilla/5.0 contact bot-research",
            "Accept": "text/calendar,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    try:
        payload = urlopen(request, timeout=20).read().decode("utf-8", errors="replace")
        BLS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BLS_CACHE_PATH.write_text(payload, encoding="utf-8")
    except Exception:
        if not BLS_CACHE_PATH.exists():
            raise
        payload = BLS_CACHE_PATH.read_text(encoding="utf-8-sig")
    wanted = {
        "Consumer Price Index": "CPI",
        "Producer Price Index": "PPI",
        "Employment Situation": "NFP",
    }
    rows: list[Event] = []
    for block in re.findall(r"BEGIN:VEVENT\s+(.*?)\s+END:VEVENT", payload, flags=re.DOTALL):
        date_match = re.search(r"^DTSTART[^:]*:(\d{8}T\d{6})\s*$", block, flags=re.MULTILINE)
        summary_match = re.search(r"^SUMMARY:(.+?)\s*$", block, flags=re.MULTILINE)
        if not date_match or not summary_match:
            continue
        name = summary_match.group(1).strip()
        category = wanted.get(name)
        if not category:
            continue
        local = datetime.strptime(date_match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=ET)
        event = Event(name, category, local.astimezone(UTC))
        if start <= event.utc <= end:
            rows.append(event)
    return rows


def _fomc_events(start: datetime, end: datetime) -> list[Event]:
    # Statement dates from the Federal Reserve's 2025-2026 meeting calendars.
    statement_dates = [
        "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17",
        "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18",
        "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
        "2026-10-28", "2026-12-09",
    ]
    rows = [_event("FOMC Statement", "FOMC", date, "14:00") for date in statement_dates]
    return [row for row in rows if start <= row.utc <= end]


def high_impact_events(start: datetime, end: datetime) -> list[Event]:
    rows = _download_bls_events(start, end) + _fomc_events(start, end)
    unique = {(row.category, row.utc): row for row in rows}
    return sorted(unique.values(), key=lambda row: row.utc)


def _rates_chunked(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=60))
        raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, cursor, chunk_end)
        if raw is not None and len(raw):
            chunks.append(pd.DataFrame(raw))
        cursor = chunk_end + timedelta(seconds=1)
    if not chunks:
        return pd.DataFrame()
    frame = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True).dt.as_unit("ns")
    return enrich(frame)


def _entry(frame: pd.DataFrame, event_idx: int, strategy: str, threshold: float) -> tuple[int, str, float] | None:
    if event_idx < 20 or event_idx + 4 >= len(frame):
        return None
    pre = frame.iloc[event_idx - 1]
    event = frame.iloc[event_idx]
    atr = float(pre["atr14"])
    impulse = float(event["close"]) - float(pre["close"])
    if atr <= 0 or abs(impulse) < atr * threshold:
        return None
    direction = "buy" if impulse > 0 else "sell"
    event_range = max(0.00001, float(event["high"]) - float(event["low"]))
    event_body_ratio = abs(float(event["close"]) - float(event["open"])) / event_range

    if strategy == "momentum_5m":
        if event_body_ratio < 0.55:
            return None
        return event_idx, direction, float(event["close"])

    if strategy == "momentum_10m":
        confirm = frame.iloc[event_idx + 1]
        confirm_move = float(confirm["close"]) - float(event["close"])
        if confirm_move * impulse <= 0:
            return None
        return event_idx + 1, direction, float(confirm["close"])

    if strategy == "range_breakout":
        for idx in range(event_idx + 1, event_idx + 4):
            row = frame.iloc[idx]
            if direction == "buy" and float(row["close"]) > float(event["high"]):
                return idx, direction, float(row["close"])
            if direction == "sell" and float(row["close"]) < float(event["low"]):
                return idx, direction, float(row["close"])
        return None

    if strategy == "pullback":
        retrace = abs(impulse) * 0.35
        for idx in range(event_idx + 1, event_idx + 4):
            row = frame.iloc[idx]
            if direction == "buy" and float(row["low"]) <= float(event["close"]) - retrace and float(row["close"]) > float(row["open"]):
                return idx, direction, float(row["close"])
            if direction == "sell" and float(row["high"]) >= float(event["close"]) + retrace and float(row["close"]) < float(row["open"]):
                return idx, direction, float(row["close"])
        return None

    if strategy == "fade":
        confirm = frame.iloc[event_idx + 1]
        reversal = float(confirm["close"]) - float(event["close"])
        if reversal * impulse >= 0 or abs(reversal) < abs(impulse) * 0.35:
            return None
        side = "sell" if direction == "buy" else "buy"
        return event_idx + 1, side, float(confirm["close"])
    return None


def _trade(frame: pd.DataFrame, entry_idx: int, side: str, entry: float, atr: float, sl_atr: float, tp_atr: float, be_at_r: float, friction: float) -> dict[str, Any]:
    risk = atr * sl_atr
    reward = atr * tp_atr
    sl = entry - risk if side == "buy" else entry + risk
    tp = entry + reward if side == "buy" else entry - reward
    be_armed = False
    end_idx = min(len(frame) - 1, entry_idx + 12)
    exit_price = float(frame.iloc[end_idx]["close"])
    reason = "time"
    for idx in range(entry_idx + 1, end_idx + 1):
        row = frame.iloc[idx]
        high, low = float(row["high"]), float(row["low"])
        advance = high - entry if side == "buy" else entry - low
        if be_at_r > 0 and advance >= risk * be_at_r:
            be_armed = True
            sl = max(sl, entry) if side == "buy" else min(sl, entry)
        sl_hit = low <= sl if side == "buy" else high >= sl
        tp_hit = high >= tp if side == "buy" else low <= tp
        if sl_hit:
            exit_price, reason = sl, "be" if be_armed and sl == entry else "sl"
            break
        if tp_hit:
            exit_price, reason = tp, "tp"
            break
    gross = exit_price - entry if side == "buy" else entry - exit_price
    net = gross - friction
    return {"net_points": net, "gross_points": gross, "reason": reason, "r": net / risk if risk else 0.0}


def _evaluate(frame: pd.DataFrame, events: list[Event], strategy: str, threshold: float, sl_atr: float, tp_atr: float, be_at_r: float, friction: float) -> dict[str, Any]:
    times = frame["time"]
    trades: list[dict[str, Any]] = []
    for event in events:
        matches = frame.index[times == pd.Timestamp(event.utc)]
        if len(matches) == 0:
            continue
        event_idx = int(matches[0])
        signal = _entry(frame, event_idx, strategy, threshold)
        if signal is None:
            continue
        entry_idx, side, entry_price = signal
        atr = float(frame.iloc[event_idx - 1]["atr14"])
        result = _trade(frame, entry_idx, side, entry_price, atr, sl_atr, tp_atr, be_at_r, friction)
        trades.append({"event": event.category, "time": event.utc.isoformat(), "side": side, **result})
    pnl = sum(row["net_points"] for row in trades)
    wins = sum(1 for row in trades if row["net_points"] > 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in trades:
        equity += row["net_points"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "trades": len(trades),
        "wins": wins,
        "win_rate_pct": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "net_points": round(pnl, 2),
        "average_r": round(sum(row["r"] for row in trades) / len(trades), 3) if trades else 0.0,
        "max_drawdown_points": round(max_dd, 2),
        "details": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--xau-friction", type=float, default=1.0)
    parser.add_argument("--nas-friction", type=float, default=8.0)
    parser.add_argument("--output", default="data_vantage/high_impact_news_backtest_180d.json")
    args = parser.parse_args()
    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    all_events = high_impact_events(start, end)
    split_idx = max(1, int(len(all_events) * 0.67))
    train_events, test_events = all_events[:split_idx], all_events[split_idx:]
    instruments = {
        "XAUUSD+": {"friction": max(0.0, args.xau_friction)},
        "NAS100": {"friction": max(0.0, args.nas_friction)},
    }
    output: dict[str, Any] = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "events": [{"name": row.name, "category": row.category, "utc": row.utc.isoformat()} for row in all_events],
        "split": {"training_events": len(train_events), "testing_events": len(test_events)},
        "assumptions": {"bar": "M5", "same_bar_rule": "SL before TP", "max_hold_minutes": 60, "friction_price_units": {key: value["friction"] for key, value in instruments.items()}},
        "instruments": {},
    }
    for requested, assumptions in instruments.items():
        symbol = ensure_symbol(requested)
        frame = _rates_chunked(symbol, start - timedelta(days=3), end + timedelta(hours=1))
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        profit_per_point_001 = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 0.01, float(tick.ask), float(tick.ask) + 1.0)
        rows = []
        for strategy in ["momentum_5m", "momentum_10m", "range_breakout", "pullback", "fade"]:
            for threshold in [0.2, 0.4, 0.6, 0.8, 1.0]:
                for sl_atr in [0.75, 1.0, 1.25, 1.5]:
                    for tp_atr in [1.0, 1.5, 2.0, 2.5, 3.0]:
                        for be_at_r in [0.0, 1.0]:
                            train = _evaluate(frame, train_events, strategy, threshold, sl_atr, tp_atr, be_at_r, assumptions["friction"])
                            test = _evaluate(frame, test_events, strategy, threshold, sl_atr, tp_atr, be_at_r, assumptions["friction"])
                            if train["trades"] + test["trades"] < 5 or test["trades"] < 2:
                                continue
                            rows.append({"strategy": strategy, "threshold_atr": threshold, "sl_atr": sl_atr, "tp_atr": tp_atr, "be_at_r": be_at_r, "training": train, "testing": test})
        robust = [row for row in rows if row["training"]["net_points"] > 0 and row["testing"]["net_points"] > 0]
        robust.sort(key=lambda row: (row["testing"]["average_r"], row["training"]["average_r"], row["testing"]["net_points"]), reverse=True)
        robust_min_sample = [
            row for row in robust
            if row["training"]["trades"] >= 8 and row["testing"]["trades"] >= 4
        ]
        robust_min_sample.sort(
            key=lambda row: (
                min(row["training"]["average_r"], row["testing"]["average_r"]),
                row["training"]["net_points"] + row["testing"]["net_points"],
            ),
            reverse=True,
        )
        best_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for strategy_name in ["momentum_5m", "momentum_10m", "range_breakout", "pullback", "fade"]:
            subset = [row for row in rows if row["strategy"] == strategy_name]
            subset.sort(
                key=lambda row: (
                    min(row["training"]["net_points"], row["testing"]["net_points"]),
                    row["training"]["net_points"] + row["testing"]["net_points"],
                ),
                reverse=True,
            )
            best_by_strategy[strategy_name] = subset[:3]
        output["instruments"][symbol] = {
            "bars": len(frame),
            "first_bar": frame.iloc[0]["time"].isoformat(),
            "last_bar": frame.iloc[-1]["time"].isoformat(),
            "current_spread": round(float(tick.ask) - float(tick.bid), 5),
            "profit_usd_per_price_unit_001": round(float(profit_per_point_001 or 0.0), 5),
            "tested_combinations": len(rows),
            "robust_combinations": len(robust),
            "robust_min_sample_combinations": len(robust_min_sample),
            "best_robust": robust[:20],
            "best_robust_min_sample": robust_min_sample[:20],
            "best_by_strategy": best_by_strategy,
        }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(path)
    shutdown()


if __name__ == "__main__":
    main()
