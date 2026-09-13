from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _is_phoenix_source, _parse_signal, _phoenix_direction_hint
from backtest_current_three_leg_projection import _dialog_variants, _variants
from optimize_funded_multi_leg_30d import _entries_for_mode, _profit, _simulate_leg


TARGET_PLANS = {
    "tp1_all": (1, 1, 1),
    "tp1_tp1_tp2": (1, 1, 2),
    "tp1_tp2_tp3": (1, 2, 3),
    "tp1_tp2_tp4": (1, 2, 4),
    "tp1_tp2_tp5": (1, 2, 5),
    "tp1_tp3_tp5": (1, 3, 5),
}

PROTECT_PLANS = {
    "none": ("none", "none", "none"),
    "be_after_tp1": ("none", "be", "be"),
    "be_after_tp2": ("none", "be_after_tp2", "be_after_tp2"),
    "be_after_tp3": ("none", "be_after_tp3", "be_after_tp3"),
    "tp1_after_tp2": ("none", "tp1_after_tp2", "tp1_after_tp2"),
    "tp1_after_tp3": ("none", "tp1_after_tp3", "tp1_after_tp3"),
    "progressive_ladder": ("none", "progressive_ladder", "progressive_ladder"),
    "delayed_ladder": ("none", "delayed_ladder", "delayed_ladder"),
}

SL_PROFILES = {
    "signal": {"gold": 0.0, "nas100": 0.0, "us30": 0.0},
    "tight": {"gold": 2.5, "nas100": 30.0, "us30": 60.0},
    "medium": {"gold": 4.0, "nas100": 60.0, "us30": 120.0},
    "wide": {"gold": 6.0, "nas100": 100.0, "us30": 200.0},
}

ENTRY_MODES = ("market_between_entry_tp1", "nearest_pending_15m")


@dataclass
class SignalRow:
    dt: datetime
    message_id: int
    signal: Any
    rates: pd.DataFrame
    symbol: str
    start_idx: int
    market: float
    spread_price: float


def _tf(name: str) -> int:
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}[name]


def _rates(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, _tf(timeframe), start, end)
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"No {timeframe} rates for {symbol}")
    frame = pd.DataFrame(raw)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return enrich(frame.sort_values("time").reset_index(drop=True))


def _safe(text: str) -> str:
    return (text or "").encode("ascii", "ignore").decode("ascii").strip() or "private"


def _summary(values: list[float]) -> dict[str, Any]:
    positive = sum(value > 0.01 for value in values)
    negative = sum(value < -0.01 for value in values)
    flat = len(values) - positive - negative
    decided = positive + negative
    return {
        "signals": len(values),
        "positive": positive,
        "negative": negative,
        "flat": flat,
        "win_rate_pct": round(100.0 * positive / max(1, decided), 2),
        "non_loss_rate_pct": round(100.0 * (positive + flat) / max(1, len(values)), 2),
        "pnl_001_per_leg": round(sum(values), 2),
    }


def _wilson_lower(positive: int, total: int, z: float = 1.645) -> float:
    if total <= 0:
        return 0.0
    p = positive / total
    denom = 1.0 + (z * z / total)
    center = p + (z * z / (2.0 * total))
    margin = z * math.sqrt((p * (1.0 - p) / total) + (z * z / (4.0 * total * total)))
    return (center - margin) / denom


def _fingerprint(row: SignalRow) -> str:
    bucket = row.dt.replace(minute=(row.dt.minute // 10) * 10, second=0, microsecond=0).isoformat()
    entries = [float(value) for value in row.signal.entries if float(value or 0.0) > 0]
    entry = entries[0] if entries else float(row.signal.entry or row.market)
    tp1 = float(row.signal.tps[0]) if row.signal.tps else 0.0
    scale = 1 if row.signal.asset == "gold" else 10
    return f"{bucket}|{row.signal.asset}|{row.signal.side}|{round(entry / scale)}|{round(tp1 / scale)}"


async def _fetch_channel_signals(
    client: TelegramClient,
    dialog: Any,
    cutoff: datetime,
    rates_by_asset: dict[str, dict[str, Any]],
    offset_minutes: int,
) -> list[SignalRow]:
    title = str(getattr(dialog, "title", "") or getattr(dialog.entity, "username", "") or dialog.id)
    chat_id = int(getattr(dialog, "id", 0) or 0)
    raw_messages = []
    async for message in client.iter_messages(dialog.entity, limit=6000):
        if message.date and message.date < cutoff:
            break
        if str(getattr(message, "raw_text", "") or "").strip():
            raw_messages.append(message)
    raw_messages.reverse()

    hint_side = None
    hint_time = None
    output: list[SignalRow] = []
    for message in raw_messages:
        text = str(getattr(message, "raw_text", "") or "")
        is_phoenix = _is_phoenix_source(chat_id, title)
        announced = _phoenix_direction_hint(text) if is_phoenix else None
        if announced:
            hint_side = announced
            hint_time = message.date
        fresh_hint = None
        if hint_side and hint_time and message.date - hint_time <= timedelta(minutes=20):
            fresh_hint = hint_side
        parsed = _parse_signal(
            text,
            f"{chat_id}:{int(message.id or 0)}",
            chat_id,
            title,
            "",
            int(message.id or 0),
            side_hint=fresh_hint if is_phoenix else None,
        )
        if parsed is None or parsed.asset not in rates_by_asset:
            continue
        if is_phoenix:
            hint_side = None
            hint_time = None
        asset_data = rates_by_asset[parsed.asset]
        rates = asset_data["rates"]
        candle_time = pd.Timestamp(message.date) + pd.Timedelta(minutes=offset_minutes)
        idx = int(rates["time"].searchsorted(candle_time, side="left"))
        if idx >= len(rates):
            continue
        output.append(
            SignalRow(
                dt=message.date.astimezone(UTC),
                message_id=int(message.id or 0),
                signal=parsed,
                rates=rates,
                symbol=str(asset_data["symbol"]),
                start_idx=idx,
                market=float(rates.iloc[idx]["close"]),
                spread_price=float(asset_data["spread_price"]),
            )
        )
    return output


def _effective_protect(target: int, protect: str) -> str:
    if target <= 1:
        return "none"
    if protect == "be_after_tp2" and target <= 2:
        return "none"
    if protect == "be_after_tp3" and target <= 3:
        return "none"
    if protect == "tp1_after_tp2" and target <= 2:
        return "none"
    if protect == "tp1_after_tp3" and target <= 3:
        return "none"
    if protect == "delayed_ladder" and target <= 3:
        return "none"
    return protect


def _precompute_legs(rows: list[SignalRow]) -> dict[tuple[int, str, str, int, str], float | None]:
    needed = {
        (int(target), _effective_protect(int(target), str(protect)))
        for targets in TARGET_PLANS.values()
        for protects in PROTECT_PLANS.values()
        for target, protect in zip(targets, protects)
    }
    cache: dict[tuple[int, str, str, int, str], float | None] = {}
    for row_index, row in enumerate(rows):
        for entry_mode in ENTRY_MODES:
            entries = _entries_for_mode(row.signal, row.market, entry_mode)
            if not entries:
                for sl_profile in SL_PROFILES:
                    for target, protect in needed:
                        cache[(row_index, entry_mode, sl_profile, target, protect)] = None
                continue
            entry, pending = entries[0]
            for sl_profile in SL_PROFILES:
                for target, protect in needed:
                    result = _simulate_leg(
                        row.signal,
                        row.symbol,
                        row.rates,
                        row.start_idx,
                        float(entry),
                        bool(pending),
                        target,
                        protect,
                        float(SL_PROFILES[sl_profile][row.signal.asset]),
                        15.0,
                        24.0,
                    )
                    key = (row_index, entry_mode, sl_profile, target, protect)
                    if result.get("status") == "skip":
                        cache[key] = None
                        continue
                    gross = _profit(row.symbol, row.signal.side, 0.01, float(result["entry"]), float(result["exit"]))
                    spread = abs(_profit(row.symbol, "buy", 0.01, float(result["entry"]), float(result["entry"]) + row.spread_price))
                    cache[key] = round(float(gross) - float(spread), 2)
    return cache


def _evaluate_config(
    rows: list[SignalRow],
    cache: dict[tuple[int, str, str, int, str], float | None],
    target_plan: tuple[int, ...],
    protect_plan: tuple[str, ...],
    sl_profile: str,
    entry_mode: str,
) -> list[float]:
    pnls: list[float] = []
    for row_index, _row in enumerate(rows):
        signal_pnl = 0.0
        activated = 0
        for target, protect in zip(target_plan, protect_plan):
            effective = _effective_protect(int(target), str(protect))
            value = cache[(row_index, entry_mode, sl_profile, int(target), effective)]
            if value is None:
                continue
            activated += 1
            signal_pnl += float(value)
        pnls.append(round(signal_pnl, 2) if activated else 0.0)
    return pnls


def _calibrate_channel(rows: list[SignalRow]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row.dt)
    cache = _precompute_legs(rows)
    split = max(1, min(len(rows) - 1, int(len(rows) * 0.70))) if len(rows) > 1 else 1
    runs = []
    for target_name, targets in TARGET_PLANS.items():
        for protect_name, protects in PROTECT_PLANS.items():
            for sl_profile in SL_PROFILES:
                for entry_mode in ENTRY_MODES:
                    values = _evaluate_config(rows, cache, targets, protects, sl_profile, entry_mode)
                    train = _summary(values[:split])
                    holdout = _summary(values[split:])
                    full = _summary(values)
                    runs.append(
                        {
                            "target_name": target_name,
                            "target_plan": targets,
                            "protect_name": protect_name,
                            "protect_plan": protects,
                            "sl_profile": sl_profile,
                            "entry_mode": entry_mode,
                            "train": train,
                            "holdout": holdout,
                            "full": full,
                        }
                    )
    robust = [
        run for run in runs
        if run["train"]["pnl_001_per_leg"] > 0
        and run["holdout"]["pnl_001_per_leg"] > 0
        and run["holdout"]["signals"] >= 2
    ]
    if robust:
        winner = max(
            robust,
            key=lambda run: (
                min(run["train"]["non_loss_rate_pct"], run["holdout"]["non_loss_rate_pct"]),
                run["full"]["pnl_001_per_leg"],
                run["holdout"]["pnl_001_per_leg"],
            ),
        )
    else:
        winner = max(
            runs,
            key=lambda run: (
                run["full"]["pnl_001_per_leg"],
                min(run["train"]["pnl_001_per_leg"], run["holdout"]["pnl_001_per_leg"]),
                run["full"]["non_loss_rate_pct"],
            ),
        )
    full = winner["full"]
    lcb = _wilson_lower(int(full["positive"] + full["flat"]), int(full["signals"]))
    qualified = bool(
        len(rows) >= 8
        and winner["train"]["pnl_001_per_leg"] > 0
        and winner["holdout"]["pnl_001_per_leg"] > 0
        and winner["holdout"]["non_loss_rate_pct"] >= 60.0
    )
    return {
        "signals": len(rows),
        "train_signals": split,
        "holdout_signals": len(rows) - split,
        "robust": bool(robust),
        "qualified": qualified,
        "confidence_non_loss_lcb": round(lcb * 100.0, 2),
        "best": winner,
        "top5": sorted(
            runs,
            key=lambda run: (run["full"]["pnl_001_per_leg"], run["full"]["non_loss_rate_pct"]),
            reverse=True,
        )[:5],
        "fingerprints": sorted({_fingerprint(row) for row in rows}),
    }


async def main() -> None:
    global TARGET_PLANS, PROTECT_PLANS, SL_PROFILES
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--timeframe", choices=["M1", "M5", "M15"], default="M5")
    parser.add_argument("--broker-time-offset-minutes", type=int, default=180)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--tp1-only", action="store_true", help="Test only three TP1 legs without BE")
    parser.add_argument(
        "--gold-sl-caps",
        default="",
        help="Comma-separated XAU SL caps; NAS100 and US30 caps are scaled proportionally",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.tp1_only:
        TARGET_PLANS = {"tp1_all": (1, 1, 1)}
        PROTECT_PLANS = {"none": ("none", "none", "none")}
    if args.gold_sl_caps:
        caps = [max(0.5, float(value.strip())) for value in args.gold_sl_caps.split(",") if value.strip()]
        SL_PROFILES = {
            f"cap_{cap:g}": {"gold": cap, "nas100": cap * 12.0, "us30": cap * 24.0}
            for cap in caps
        }
    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        now = datetime.now(UTC)
        offset = timedelta(minutes=int(args.broker_time_offset_minutes))
        cutoff = now - timedelta(days=int(args.days))
        symbol_candidates = {"gold": cfg.symbol, "nas100": "NAS100", "us30": "DJ30"}
        rates_by_asset: dict[str, dict[str, Any]] = {}
        for asset, candidate in symbol_candidates.items():
            try:
                symbol = ensure_symbol(candidate)
                rates = _rates(symbol, args.timeframe, cutoff + offset - timedelta(days=2), now + offset + timedelta(hours=26))
            except Exception as exc:
                print(f"skip asset {asset}: {type(exc).__name__}: {exc}", flush=True)
                continue
            info = mt5.symbol_info(symbol)
            point = float(getattr(info, "point", 0.01) or 0.01)
            spread_price = float(rates["spread"].median()) * point if "spread" in rates.columns else 0.0
            rates_by_asset[asset] = {"symbol": symbol, "rates": rates, "spread_price": spread_price}

        source = cfg.data_dir / f"{cfg.telegram_session_name}.session"
        session_copy = cfg.data_dir / f"{cfg.telegram_session_name}_calibrate25.session"
        if source.exists():
            shutil.copy2(source, session_copy)
        client = TelegramClient(str(session_copy.with_suffix("").resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram session is not authorized")
            wanted = [_variants(item) for item in cfg.telegram_watch_channels]
            dialogs = [dialog async for dialog in client.iter_dialogs() if any(_dialog_variants(dialog) & token for token in wanted)]
            print(f"matched dialogs={len(dialogs)} watched={len(wanted)}", flush=True)
            channel_results = []
            for index, dialog in enumerate(dialogs, start=1):
                rows = await _fetch_channel_signals(client, dialog, cutoff, rates_by_asset, int(args.broker_time_offset_minutes))
                title = str(getattr(dialog, "title", "") or dialog.id)
                username = str(getattr(dialog.entity, "username", "") or "")
                if not rows:
                    channel_results.append({"id": int(dialog.id), "title": title, "safe_title": _safe(title), "username": username, "signals": 0, "qualified": False})
                    print(f"{index}/{len(dialogs)} {_safe(title)}: no parsed signals", flush=True)
                    continue
                result = _calibrate_channel(rows)
                result.update({"id": int(dialog.id), "title": title, "safe_title": _safe(title), "username": username, "link": f"https://t.me/{username}" if username else ""})
                channel_results.append(result)
                best = result["best"]
                print(
                    f"{index}/{len(dialogs)} {_safe(title)}: signals={len(rows)} qualified={result['qualified']} "
                    f"plan={best['target_name']}/{best['protect_name']}/{best['sl_profile']} "
                    f"pnl={best['full']['pnl_001_per_leg']} nonloss={best['full']['non_loss_rate_pct']}",
                    flush=True,
                )
        finally:
            await client.disconnect()

        calibrated = [row for row in channel_results if row.get("best")]
        calibrated.sort(
            key=lambda row: (
                bool(row.get("qualified")),
                float(row.get("confidence_non_loss_lcb", 0.0)),
                float(row["best"]["full"]["pnl_001_per_leg"]),
            ),
            reverse=True,
        )
        selected = []
        for row in calibrated:
            fingerprints = set(row.get("fingerprints", []))
            mirror = None
            for kept in selected:
                kept_fp = set(kept.get("fingerprints", []))
                overlap = len(fingerprints & kept_fp) / max(1, min(len(fingerprints), len(kept_fp)))
                if overlap >= 0.70:
                    mirror = {"id": kept["id"], "title": kept["title"], "overlap_pct": round(overlap * 100.0, 2)}
                    break
            if mirror:
                row["mirror_of"] = mirror
                continue
            if len(selected) < int(args.top):
                selected.append(row)

        payload = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "days": int(args.days),
            "timeframe": args.timeframe,
            "broker_time_offset_minutes": int(args.broker_time_offset_minutes),
            "watched_configured": len(cfg.telegram_watch_channels),
            "dialogs_matched": len(channel_results),
            "tested_configs_per_channel": len(TARGET_PLANS) * len(PROTECT_PLANS) * len(SL_PROFILES) * len(ENTRY_MODES),
            "selected_count": len(selected),
            "selected": selected,
            "all_channels": channel_results,
            "note": "Three legs per signal. Selection requires train and holdout profit when enough data; mirrors >=70% are excluded.",
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"selected_count": len(selected), "selected": [{"title": row["safe_title"], "signals": row["signals"], "qualified": row["qualified"], "best": row["best"]} for row in selected]}, ensure_ascii=True), flush=True)
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
