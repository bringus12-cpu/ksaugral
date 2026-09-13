from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, UserAlreadyParticipantError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings
from app.indicators import enrich
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.strategy_objective import load_analysis_objective, rank_profit_win
from app.telegram_signal_bot import _parse_signal
from scripts.optimize_funded_multi_leg_30d import _entries_for_mode, _profit, _simulate_leg


SUPPORTED_ASSETS = ("gold", "nas100", "us30")


QUERIES = (
    "xauusd signals",
    "xauusd signal",
    "xauusd free signals",
    "xauusd vip signals",
    "xauusd trading signals",
    "xauusd scalping signals",
    "xauusd gold signals",
    "xauusd forex signals",
    "xauusd sniper signals",
    "xauusd premium signals",
    "gold signals",
    "gold signal",
    "gold free signals",
    "gold vip signals",
    "gold forex signals",
    "gold trading signals",
    "gold scalping signals",
    "gold sniper signals",
    "gold premium signals",
    "forex gold signals",
    "forex xauusd",
    "forex xauusd signals",
    "bullion signals",
    "xau trading",
    "gold trader",
    "gold pips",
    "gold killer",
    "gold hunter",
    "gold master",
    "gold pro trader",
    "nas100 xauusd signals",
    "nas100 signals",
    "nasdaq signals",
    "nasdaq 100 signals",
    "us100 signals",
    "us30 signals",
    "dow jones signals",
    "indices signals",
    "nas100 us30 signals",
    "btc xauusd signals",
)

WEB_SEED_USERNAMES = (
    "nas100us100",
    "Nas100Us30Signals",
    "us30nas100signals",
    "RealXAirdrops",
    "nas100sniper",
    "Free_signals",
    "xauusd_trading_scalping_signals",
    "sureshotfxgoldsignal1",
    "Gold_scalping_signals",
    "goldsignals_now",
    "Free_Nasdaqsignals_team",
    "altsignals_io_official",
    "Xauusd_Gold_Scalping_Signal",
    "goldforexsignalsoriginal",
    "GoldFxSign",
    "gold_fx_indices_signals_pro",
    "UnitedSignalsFX",
    "train2trade1",
    "goldsignalsvip_S",
)

NAME_RE = re.compile(r"(?:xau|gold|bullion|nas\s*100|nas100|nasdaq|us\s*100|us100|us\s*30|us30|dow|indices)", re.I)
TEXT_RE = re.compile(r"(?:xau\s*/?\s*usd|xauusd|gold|nas\s*100|nas100|nasdaq|us\s*100|us100|us\s*30|us30|dow)", re.I)
SIGNAL_HINT_RE = re.compile(
    r"(?:buy|sell|long|short).{0,120}(?:tp|take\s*profit|target|sl|stop\s*loss)|"
    r"(?:tp|take\s*profit|target).{0,120}(?:sl|stop\s*loss)",
    re.I | re.S,
)


def _tf(name: str) -> int:
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}[name.upper()]


def _rates(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    raw = mt5.copy_rates_range(symbol, _tf(timeframe), start, end)
    if raw is not None and len(raw) > 0:
        frame = pd.DataFrame(raw)
    else:
        chunks: list[pd.DataFrame] = []
        for offset in range(0, 150000, 10000):
            chunk = mt5.copy_rates_from_pos(symbol, _tf(timeframe), offset, 10000)
            if chunk is None or len(chunk) == 0:
                break
            chunk_frame = pd.DataFrame(chunk)
            chunks.append(chunk_frame)
            oldest = datetime.fromtimestamp(int(chunk_frame["time"].min()), UTC)
            if oldest <= start:
                break
            if len(chunk) < 10000:
                break
        if not chunks:
            raise RuntimeError(f"No {timeframe} rates for {symbol}")
        frame = pd.concat(chunks, ignore_index=True)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    frame = frame[(frame["time"] >= pd.Timestamp(start)) & (frame["time"] <= pd.Timestamp(end))]
    if frame.empty:
        raise RuntimeError(f"No {timeframe} rates for {symbol} in requested range")
    frame = frame.drop_duplicates(subset=["time"], keep="last").sort_values("time").reset_index(drop=True)
    return frame


def _channel_id(channel: Any) -> int:
    raw = int(getattr(channel, "id", 0) or 0)
    return raw if str(raw).startswith("-100") else -1000000000000 - raw


def _title(channel: Any) -> str:
    return str(getattr(channel, "title", "") or getattr(channel, "username", "") or getattr(channel, "id", ""))


def _username(channel: Any) -> str:
    return str(getattr(channel, "username", "") or "")


def _safe_title(text: str) -> str:
    return (text or "").encode("ascii", "ignore").decode("ascii").strip()[:80] or "private"


def _symbol_for_asset(asset: str, cfg: Any) -> str | None:
    if asset == "gold":
        return str(cfg.symbol)
    if asset == "nas100":
        return "NAS100"
    if asset == "us30":
        return "DJ30"
    return None


async def _search(client: TelegramClient) -> dict[int, Any]:
    found: dict[int, Any] = {}
    for query in QUERIES:
        try:
            result = await client(functions.contacts.SearchRequest(q=query, limit=100))
        except FloodWaitError as exc:
            print(f"search flood wait {exc.seconds}s", flush=True)
            await asyncio.sleep(exc.seconds + 1)
            result = await client(functions.contacts.SearchRequest(q=query, limit=100))
        for chat in result.chats:
            if not isinstance(chat, types.Channel):
                continue
            username = _username(chat)
            title = _title(chat)
            if username and NAME_RE.search(f"{title} {username}"):
                found[int(chat.id)] = chat
        await asyncio.sleep(0.35)
    for username in WEB_SEED_USERNAMES:
        try:
            chat = await client.get_entity(username)
        except Exception:
            continue
        if isinstance(chat, types.Channel):
            found[int(chat.id)] = chat
    return found


async def _quick_profile(client: TelegramClient, channel: Any, cutoff: datetime, max_messages: int) -> dict[str, Any] | None:
    scanned = xau = hints = parsed = explicit_sl = 0
    parsed_by_asset: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    title = _title(channel)
    username = _username(channel)
    chat_id = _channel_id(channel)
    try:
        async for message in client.iter_messages(channel, limit=max_messages):
            if message.date and message.date < cutoff:
                break
            scanned += 1
            text = str(getattr(message, "raw_text", "") or "")
            if not text:
                continue
            if TEXT_RE.search(text):
                xau += 1
            if not SIGNAL_HINT_RE.search(text):
                continue
            hints += 1
            signal = _parse_signal(
                text,
                f"{chat_id}:{int(getattr(message, 'id', 0) or 0)}",
                chat_id,
                title,
                "",
                int(getattr(message, "id", 0) or 0),
            )
            if signal is None or signal.asset not in SUPPORTED_ASSETS:
                continue
            parsed += 1
            parsed_by_asset[str(signal.asset)] += 1
            explicit_sl += 1 if float(signal.sl or 0.0) > 0 else 0
            if len(samples) < 2:
                samples.append(
                    {
                        "id": int(getattr(message, "id", 0) or 0),
                        "date": message.date.isoformat() if message.date else "",
                        "asset": signal.asset,
                        "side": signal.side,
                        "entry": signal.entry,
                        "sl": signal.sl,
                        "tps": signal.tps[:4],
                    }
                )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "title": title, "username": username}
    if parsed < 2:
        return None
    return {
        "id": chat_id,
        "title": title,
        "username": username,
        "link": f"https://t.me/{username}" if username else "",
        "participants": int(getattr(channel, "participants_count", 0) or 0),
        "scanned": scanned,
        "xau_mentions": xau,
        "signal_hints": hints,
        "parsed": parsed,
        "parsed_by_asset": dict(parsed_by_asset),
        "explicit_sl": explicit_sl,
        "parse_rate": round(parsed / max(1, hints), 4),
        "explicit_sl_rate": round(explicit_sl / max(1, parsed), 4),
        "samples": samples,
    }


async def _simulate_channel(
    client: TelegramClient,
    channel: Any,
    rates_by_asset: dict[str, dict[str, Any]],
    cutoff: datetime,
    max_messages: int,
    target_plan: tuple[int, ...],
    spread_usd: float,
    stale_profit_minutes: float,
    holdout_start: datetime,
) -> dict[str, Any]:
    title = _title(channel)
    username = _username(channel)
    chat_id = _channel_id(channel)
    statuses = Counter()
    skip_reasons = Counter()
    parsed = candidates = simulated_legs = signal_count = 0
    p001 = 0.0
    by_target = defaultdict(Counter)
    by_asset = defaultdict(Counter)
    p001_by_asset = defaultdict(float)
    normalized_signals = 0
    basis_shifts: list[float] = []
    examples: list[dict[str, Any]] = []
    trade_events: list[dict[str, Any]] = []
    train_p001 = holdout_p001 = 0.0
    train_legs = holdout_legs = 0
    try:
        async for message in client.iter_messages(channel, limit=max_messages):
            if message.date and message.date < cutoff:
                break
            text = str(getattr(message, "raw_text", "") or "")
            if not SIGNAL_HINT_RE.search(text):
                continue
            candidates += 1
            signal = _parse_signal(
                text,
                f"{chat_id}:{int(getattr(message, 'id', 0) or 0)}",
                chat_id,
                title,
                "",
                int(getattr(message, "id", 0) or 0),
            )
            if signal is None or signal.asset not in SUPPORTED_ASSETS:
                continue
            parsed += 1
            asset_rates = rates_by_asset.get(str(signal.asset))
            if not asset_rates:
                skip_reasons[f"unsupported_asset:{signal.asset}"] += 1
                continue
            symbol = str(asset_rates["symbol"])
            rates = asset_rates["rates"]
            idx = int(rates["time"].searchsorted(pd.Timestamp(message.date), side="left"))
            if idx >= len(rates):
                skip_reasons["no_rates_after_message"] += 1
                continue
            # The selected bar has not closed at entry; only its opening quote is available.
            market = float(rates.iloc[idx]["open"])
            signal, basis_shift = _normalize_signal_to_market(signal, market)
            if basis_shift:
                normalized_signals += 1
                basis_shifts.append(float(basis_shift))
            provider_entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
            provider_entries = provider_entries or [float(signal.entry or market)]
            near_threshold = {"gold": 5.0, "nas100": 80.0, "us30": 120.0}[str(signal.asset)]
            market_near_provider = min(abs(market - value) for value in provider_entries) <= near_threshold
            entry_mode = (
                "market_between_entry_tp1"
                if signal.order_kind == "market" and market_near_provider
                else "nearest_pending_15m"
            )
            entries = _entries_for_mode(signal, market, entry_mode)
            if not entries:
                skip_reasons["no_entry_before_tp1"] += 1
                continue
            signal_count += 1
            for entry, pending in entries:
                result_cache: dict[int, dict[str, Any]] = {}
                for target in target_plan:
                    target = int(target)
                    if target not in result_cache:
                        result_cache[target] = _simulate_leg(
                            signal,
                            symbol,
                            rates,
                            idx,
                            entry,
                            pending,
                            target,
                            "none" if target == 1 else "be",
                            0.0,
                            15.0,
                            72.0,
                            stale_profit_minutes=stale_profit_minutes,
                            initial_market_price=market,
                        )
                    result = result_cache[target]
                    status = str(result.get("status"))
                    if status == "skip":
                        skip_reasons[str(result.get("reason") or "skip")] += 1
                        continue
                    simulated_legs += 1
                    statuses[status] += 1
                    by_target[int(target)][status] += 1
                    by_asset[str(signal.asset)][status] += 1
                    profit = _profit(symbol, signal.side, 0.01, float(result["entry"]), float(result["exit"]))
                    if spread_usd > 0:
                        spread_cost = abs(_profit(symbol, "buy", 0.01, float(result["entry"]), float(result["entry"]) + spread_usd))
                        profit -= spread_cost
                    p001 += profit
                    if message.date < holdout_start:
                        train_p001 += profit
                        train_legs += 1
                    else:
                        holdout_p001 += profit
                        holdout_legs += 1
                    p001_by_asset[str(signal.asset)] += profit
                    entry_idx = int(result.get("entry_idx", idx))
                    exit_idx = int(result["exit_idx"])
                    trade_events.append(
                        {
                            "channel": username or str(chat_id),
                            "chat_id": chat_id,
                            "message_id": int(getattr(message, "id", 0) or 0),
                            "asset": str(signal.asset),
                            "symbol": symbol,
                            "side": signal.side,
                            "target": target,
                            "status": status,
                            "opened": rates.iloc[entry_idx]["time"].isoformat(),
                            "closed": rates.iloc[exit_idx]["time"].isoformat(),
                            "entry": round(float(result["entry"]), 5),
                            "initial_sl": round(float(signal.sl or 0.0), 5),
                            "exit": round(float(result["exit"]), 5),
                            "p001": round(profit, 6),
                        }
                    )
                    if len(examples) < 5:
                        examples.append(
                            {
                                "message_id": int(getattr(message, "id", 0) or 0),
                                "date": message.date.isoformat() if message.date else "",
                                "asset": signal.asset,
                                "symbol": symbol,
                                "status": status,
                                "target": target,
                                "side": signal.side,
                                "entry": round(float(result["entry"]), 2),
                                "exit": round(float(result["exit"]), 2),
                                "p001": round(profit, 4),
                            }
                        )
    except Exception as exc:
        return {"title": title, "username": username, "error": f"{type(exc).__name__}: {exc}"}
    wins = int(statuses["win"])
    losses = int(statuses["loss"])
    be = int(statuses["be"])
    total = simulated_legs
    non_loss = wins + be
    return {
        "id": chat_id,
        "title": title,
        "safe_title": _safe_title(title),
        "username": username,
        "link": f"https://t.me/{username}" if username else "",
        "candidates": candidates,
        "parsed": parsed,
        "signals_used": signal_count,
        "legs": total,
        "wins": wins,
        "losses": losses,
        "be": be,
        "timeouts": int(statuses["timeout"]),
        "win_rate": round(wins / max(1, total) * 100.0, 2),
        "non_loss_rate": round(non_loss / max(1, total) * 100.0, 2),
        "p001": round(p001, 2),
        "p099": round(p001 * 99.0, 2),
        "train": {"legs": train_legs, "p001": round(train_p001, 2)},
        "holdout": {"legs": holdout_legs, "p001": round(holdout_p001, 2)},
        "normalized_signals": normalized_signals,
        "average_basis_shift": round(sum(basis_shifts) / max(1, len(basis_shifts)), 2),
        "skip_reasons": dict(skip_reasons),
        "by_asset": {
            asset: {
                "legs": sum(counter.values()),
                "wins": int(counter["win"]),
                "losses": int(counter["loss"]),
                "be": int(counter["be"]),
                "timeouts": int(counter["timeout"]),
                "win_rate": round(float(counter["win"]) / max(1, sum(counter.values())) * 100.0, 2),
                "non_loss_rate": round(float(counter["win"] + counter["be"]) / max(1, sum(counter.values())) * 100.0, 2),
                "p001": round(float(p001_by_asset[asset]), 2),
            }
            for asset, counter in by_asset.items()
        },
        "by_target": {str(k): dict(v) for k, v in by_target.items()},
        "examples": examples,
        "trade_events": sorted(trade_events, key=lambda row: (row["opened"], row["closed"], row["channel"])),
    }


def _worth_keeping(row: dict[str, Any]) -> bool:
    if row.get("error"):
        return False
    signals = int(row.get("signals_used", 0) or 0)
    if signals < 8:
        return False
    if float(row.get("p001", 0.0) or 0.0) <= 0:
        return False
    if float(row.get("non_loss_rate", 0.0) or 0.0) < 68.0:
        return False
    if int(row.get("holdout", {}).get("legs", 0) or 0) < 2:
        return False
    if float(row.get("holdout", {}).get("p001", 0.0) or 0.0) <= 0.0:
        return False
    return True


def _profile_key(item: dict[str, Any]) -> str:
    username = str(item.get("username", "") or "").strip()
    return username or str(int(item.get("id", 0) or 0))


def _normalize_signal_to_market(signal: Any, market: float) -> tuple[Any, float]:
    """Keep provider levels intact; quote translation creates hindsight trades."""
    return signal, 0.0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=40)
    parser.add_argument("--sessions", type=int, default=0)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--inspect-messages", type=int, default=900)
    parser.add_argument("--simulate-messages", type=int, default=2500)
    parser.add_argument("--profile-concurrency", type=int, default=12)
    parser.add_argument("--profile-timeout", type=float, default=30.0)
    parser.add_argument("--leave-bad", action="store_true")
    parser.add_argument("--skip-join", action="store_true")
    parser.add_argument("--only-tokens", default="")
    parser.add_argument("--tokens-file", default="")
    parser.add_argument("--include-configured", action="store_true")
    parser.add_argument("--exclude-report", action="append", default=[])
    parser.add_argument("--target-plan", default="1,1,2")
    parser.add_argument("--spread-usd", type=float, default=0.18)
    parser.add_argument("--stale-profit-minutes", type=float, default=30.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--session-suffix", default="")
    args = parser.parse_args()

    target_plan = tuple(int(item.strip()) for item in str(args.target_plan).split(",") if item.strip())
    if not target_plan or any(item < 1 for item in target_plan):
        raise ValueError("--target-plan must contain positive TP indexes, for example 1 or 1,1,2")
    excluded_usernames: set[str] = set()
    for raw_path in args.exclude_report:
        report_path = Path(raw_path).resolve()
        if not report_path.exists():
            continue
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        for row in payload.get("channels", []):
            username = str(row.get("username", "") or "").strip().lower()
            if username:
                excluded_usernames.add(username)

    cfg = load_settings()
    excluded_channel_ids = set()
    if not args.include_configured:
        excluded_channel_ids = {
            abs(int(token))
            for token in cfg.telegram_watch_channels
            if str(token).strip().lstrip("-").isdigit()
        }
    end = datetime.now(UTC)

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    cutoff = end - timedelta(days=int(args.days))
    if int(args.sessions) > 0:
        session_symbol = ensure_symbol(str(cfg.symbol))
        daily = mt5.copy_rates_from_pos(session_symbol, mt5.TIMEFRAME_D1, 0, int(args.sessions) + 30)
        if daily is None or len(daily) < int(args.sessions):
            raise RuntimeError(f"Only {0 if daily is None else len(daily)} D1 bars available")
        session_dates = sorted({datetime.fromtimestamp(int(row["time"]), UTC).date() for row in daily})
        cutoff = datetime.combine(session_dates[-int(args.sessions)], datetime.min.time(), tzinfo=UTC)
    holdout_start = cutoff + (end - cutoff) * 0.70
    rates_by_asset: dict[str, dict[str, Any]] = {}
    for asset in SUPPORTED_ASSETS:
        candidate = _symbol_for_asset(asset, cfg)
        if not candidate:
            continue
        try:
            print(f"loading M1 rates for {asset}/{candidate}", flush=True)
            symbol = ensure_symbol(candidate)
            rates_by_asset[asset] = {
                "symbol": symbol,
                "rates": _rates(symbol, "M1", cutoff - timedelta(days=2), end + timedelta(days=3)),
            }
        except Exception as exc:
            print(f"rates unavailable for {asset}/{candidate}: {type(exc).__name__}: {exc}", flush=True)

    source = cfg.data_dir / f"{cfg.telegram_session_name}.session"
    session_suffix = str(args.session_suffix).strip() or f"{os.getpid()}"
    session_copy = cfg.data_dir / f"{cfg.telegram_session_name}_discover50_{session_suffix}.session"
    if source.exists():
        shutil.copy2(source, session_copy)
    client = TelegramClient(str(session_copy.with_suffix("").resolve()), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    joined: list[str] = []
    join_errors: dict[str, str] = {}
    left_bad: list[str] = []
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")
        only_tokens = [item.strip().lstrip("@") for item in str(args.only_tokens).split(",") if item.strip()]
        if args.tokens_file:
            token_payload = json.loads(Path(args.tokens_file).read_text(encoding="utf-8"))
            token_values = token_payload.get("tokens", []) if isinstance(token_payload, dict) else token_payload
            only_tokens.extend(str(item).strip().lstrip("@") for item in token_values if str(item).strip())
        only_tokens = list(dict.fromkeys(only_tokens))
        if only_tokens:
            candidates = {}
            for token in only_tokens:
                try:
                    entity = await client.get_entity(int(token) if token.lstrip("-").isdigit() else token)
                except Exception as exc:
                    join_errors[token] = f"resolve {type(exc).__name__}: {exc}"
                    continue
                if isinstance(entity, types.Channel):
                    candidates[int(entity.id)] = entity
        else:
            candidates = await _search(client)
        candidates = {
            channel_id: channel
            for channel_id, channel in candidates.items()
            if abs(_channel_id(channel)) not in excluded_channel_ids
        }
        print(f"global candidates: {len(candidates)}", flush=True)

        profiles: list[dict[str, Any]] = []
        channels_by_key: dict[str, Any] = {}
        candidate_items = list(candidates.values())
        profile_semaphore = asyncio.Semaphore(max(1, int(args.profile_concurrency)))

        async def profile_one(index: int, channel: Any) -> tuple[int, Any, dict[str, Any] | None]:
            async with profile_semaphore:
                try:
                    profile = await asyncio.wait_for(
                        _quick_profile(client, channel, cutoff, int(args.inspect_messages)),
                        timeout=max(10.0, float(args.profile_timeout)),
                    )
                except TimeoutError:
                    profile = {
                        "error": f"TimeoutError: quick profile exceeded {max(10.0, float(args.profile_timeout)):.0f}s",
                        "title": _title(channel),
                        "username": _username(channel),
                    }
                await asyncio.sleep(0.08)
                return index, channel, profile

        profile_tasks = [
            asyncio.create_task(profile_one(index, channel))
            for index, channel in enumerate(candidate_items, start=1)
        ]
        completed_profiles = 0
        for task in asyncio.as_completed(profile_tasks):
            index, channel, profile = await task
            completed_profiles += 1
            if profile and not profile.get("error"):
                profiles.append(profile)
                channels_by_key[_profile_key(profile)] = channel
                print(
                    f"profile {completed_profiles}/{len(candidates)} source={index} "
                    f"{profile['username']} parsed={profile['parsed']}",
                    flush=True,
                )
            elif completed_profiles % 20 == 0:
                print(f"profile {completed_profiles}/{len(candidates)} no qualifying signal format", flush=True)

        profiles = [item for item in profiles if str(item.get("username", "") or "").lower() not in excluded_usernames]
        profiles.sort(key=lambda item: (item["parsed"], item["parse_rate"], item["participants"]), reverse=True)
        selected = profiles[: int(args.count)]
        results: list[dict[str, Any]] = []
        simulation_semaphore = asyncio.Semaphore(3)

        async def simulate_one(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            channel = channels_by_key.get(_profile_key(item))
            if channel is None:
                return index, {"username": item["username"], "error": "channel entity unavailable", "profile": item, "keep": False}
            async with simulation_semaphore:
                result = await _simulate_channel(
                    client,
                    channel,
                    rates_by_asset,
                    cutoff,
                    int(args.simulate_messages),
                    target_plan,
                    max(0.0, float(args.spread_usd)),
                    max(0.0, float(args.stale_profit_minutes)),
                    holdout_start,
                )
            result["profile"] = item
            result["keep"] = _worth_keeping(result)
            return index, result

        simulation_tasks = [
            asyncio.create_task(simulate_one(index, item))
            for index, item in enumerate(selected, start=1)
        ]
        completed_simulations = 0
        for task in asyncio.as_completed(simulation_tasks):
            index, result = await task
            completed_simulations += 1
            results.append(result)
            print(
                f"simulate {completed_simulations}/{len(selected)} source={index} {result.get('username')} "
                f"legs={result.get('legs')} wr={result.get('win_rate')} "
                f"p001={result.get('p001')} keep={result.get('keep')}",
                flush=True,
            )

        for row in results:
            username = str(row.get("username") or "")
            channel = channels_by_key.get(_profile_key(row))
            channel_label = username or str(row.get("id") or "")
            if not channel:
                continue
            if row.get("keep") and not args.skip_join:
                try:
                    await client(functions.channels.JoinChannelRequest(channel))
                    joined.append(channel_label)
                except UserAlreadyParticipantError:
                    joined.append(channel_label)
                except FloodWaitError as exc:
                    join_errors[channel_label] = f"join FloodWait {exc.seconds}s"
                    print(f"join flood wait at {channel_label}: {exc.seconds}s", flush=True)
                    break
                except Exception as exc:
                    join_errors[channel_label] = f"join {type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)
            elif args.leave_bad:
                try:
                    await client(functions.channels.LeaveChannelRequest(channel))
                    left_bad.append(channel_label)
                except FloodWaitError as exc:
                    join_errors[channel_label] = f"leave FloodWait {exc.seconds}s"
                    break
                except Exception as exc:
                    join_errors[channel_label] = f"leave {type(exc).__name__}: {exc}"
                await asyncio.sleep(0.8)
    finally:
        await client.disconnect()

    objective = load_analysis_objective()
    results = rank_profit_win(
        results,
        profit=lambda row: float(row.get("p001", 0.0) or 0.0),
        win_rate=lambda row: float(row.get("non_loss_rate", 0.0) or 0.0),
        objective=objective,
    )
    kept = [row for row in results if row.get("keep")]
    bad = [row for row in results if not row.get("keep")]
    output = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "range_utc": {"start": cutoff.isoformat(), "end": end.isoformat()},
        "sessions_requested": int(args.sessions),
        "holdout_start": holdout_start.isoformat(),
        "strategy": f"multi-asset XAU/NAS100/US30; original provider levels and SL; market only near provider zone, otherwise nearest pending; target plan {','.join(map(str, target_plan))}; TP1 original SL, later targets BE after TP1; pending expiry 15m; stale profitable exit {max(0.0, float(args.stale_profit_minutes)):.0f}m; spread {max(0.0, float(args.spread_usd)):.2f} USD",
        "target_plan": list(target_plan),
        "spread_usd": max(0.0, float(args.spread_usd)),
        "stale_profit_minutes": max(0.0, float(args.stale_profit_minutes)),
        "analysis_objective": objective.as_dict(),
        "excluded_usernames": sorted(excluded_usernames),
        "excluded_channel_ids": sorted(excluded_channel_ids),
        "symbols": {asset: row["symbol"] for asset, row in rates_by_asset.items()},
        "queries": list(QUERIES),
        "global_candidates": len(candidates),
        "qualified_profiles": len(profiles),
        "requested_count": int(args.count),
        "selected_count": len(selected),
        "joined": joined,
        "join_errors": join_errors,
        "kept_count": len(kept),
        "bad_count": len(bad),
        "left_bad": left_bad,
        "recommended_watch_channels": [row["username"] for row in kept[:20]],
        "summary": {
            "legs": sum(int(row.get("legs", 0) or 0) for row in results),
            "wins": sum(int(row.get("wins", 0) or 0) for row in results),
            "losses": sum(int(row.get("losses", 0) or 0) for row in results),
            "be": sum(int(row.get("be", 0) or 0) for row in results),
            "p001": round(sum(float(row.get("p001", 0.0) or 0.0) for row in results), 2),
            "p099": round(sum(float(row.get("p099", 0.0) or 0.0) for row in results), 2),
        },
        "channels": results,
    }
    output_path = Path(args.output) if args.output else cfg.data_dir / f"discover50_gold_{int(args.days)}d_strategy.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(output_path)
    print(
        json.dumps(
            {
                "selected": len(selected),
                "kept": len(kept),
                "bad": len(bad),
                "summary": output["summary"],
                "top": [
                    {
                        "username": row["username"],
                        "title": row["safe_title"],
                        "legs": row["legs"],
                        "win_rate": row["win_rate"],
                        "non_loss_rate": row["non_loss_rate"],
                        "p001": row["p001"],
                        "keep": row["keep"],
                    }
                    for row in results[:15]
                ],
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    shutdown()


if __name__ == "__main__":
    asyncio.run(main())
