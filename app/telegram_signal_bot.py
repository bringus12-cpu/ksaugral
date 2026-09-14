from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events

from .channel_analyzer import write_channel_report
from .config import load_settings
from .indicators import enrich
from .ghp_parser import is_ghp_source, looks_like_ghp_signal, parse_ghp_message
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    calc_loss_per_lot,
    connect,
    ensure_symbol,
    get_tick,
    get_rates_df,
    close_position,
    modify_order,
    modify_position,
    orders_by_magic,
    positions_by_magic,
    remove_order,
    send_market_order,
    send_pending_order,
    shutdown,
    symbol_info,
    trading_status,
    mt5,
)
from .risk import current_spread_points, normalize_volume
from .provider_update_agent import pending_cancel_candidate, review_provider_pending_update
from .signal_review_agent import load_review_policy, review_signal, source_family


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "telegram_signal_bot.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("xau-telegram-signal")
logging.getLogger("telethon").setLevel(logging.WARNING)


PRICE_RE = r"(\d{3,6}(?:\.\d+)?)"
PRICE_VALUE_RE = re.compile(r"\d{3,6}(?:\.\d+)?")
MIN_GOLD_PRICE = 1000.0
MAX_GOLD_PRICE = 10000.0
TP_LABEL_RE = re.compile(r"\b(?:tp|take\s*profits?|target|targets|tgt)\s*[\d¹²³⁴⁵⁶⁷⁸⁹Ă‚ÂąĂ‚Â˛Ă‚ÂłĂ˘ÂÂ´Ă˘ÂÂµĂ˘ÂÂ¶Ă˘ÂÂ·Ă˘ÂÂ¸Ă˘ÂÄ…]{0,2}\b", re.I)
ENTRY_RE = re.compile(rf"\b(?:entry|entries|enter|price|open|zone|buyzone|sellzone|now)\s*(?:price|zone)?\s*(?:[:=\-]|at)?\s*@?\s*{PRICE_RE}\b", re.I)
ENTRY_RANGE_RE = re.compile(
    rf"\b(?:entry|entries|enter|price|open|zone|buyzone|sellzone|now)\s*(?:price|zone|point)?\s*(?:[:=@\-\u2013\u2014]|at)?\s*{PRICE_RE}\s*(?:[-\u2013\u2014]|/+|_|\bto\b)\s*{PRICE_RE}\b",
    re.I,
)
BARE_ENTRY_RANGE_RE = re.compile(rf"\b{PRICE_RE}\s*(?:[-\u2013\u2014]|/+|_|\bto\b)\s*{PRICE_RE}\b", re.I)
SIDE_BARE_RANGE_RE = re.compile(
    r"\b(?:buy|buying|long|compra|sell|selling|short|venta)\b[^\d]{0,12}(\d{4,6}(?:\.\d+)?)\s*(?:[-\u2013\u2014]|/+|_|\bto\b)\s*(\d{4,6}(?:\.\d+)?)\b",
    re.I,
)
SIDE_SPACE_RANGE_RE = re.compile(
    r"\b(?:buy|buying|long|compra|sell|selling|short|venta)\b[^\r\n\d]{0,12}(\d{4,6}(?:\.\d+)?)[ \t]+(\d{4,6}(?:\.\d+)?)\b",
    re.I,
)
SIDE_DOT_RANGE_RE = re.compile(r"\b(?:buy|buying|long|compra|sell|selling|short|venta)\b[^\d]{0,12}(\d{4,6})\.(\d{4,6})\b", re.I)
INLINE_PENDING_ENTRY_RE = re.compile(rf"\b(?:buy|long|sell|short)\s+(?:limit|stop)\s+{PRICE_RE}\b", re.I)
SL_RE = re.compile(rf"\b(?:sl|s/l|s\.l\.?|stop\s*loss)\b[^\d]{{0,40}}{PRICE_RE}\b", re.I)
SIDE_RE = re.compile(r"\b(buy|buying|long|compra|sell|selling|short|venta)\b", re.I)
ORDER_KIND_RE = re.compile(r"\b(?:buy|buying|long|compra|sell|selling|short|venta)\s+(limit|limite|l[iĂ­]mite|stop)\b|\b(limit|limite|l[iĂ­]mite|stop)\s+(?:buy|buying|long|compra|sell|selling|short|venta)\b", re.I)
RELATIVE_PIPS_RE = re.compile(r"\bpips?\b", re.I)
NOISE_RE = re.compile(r"\b(tp\s*hit|tp1\s*hit|tp\s*reached|reached\s*tp|hit\s*tp|closed|close\s*trade|breakeven|b/e|update|done)\b", re.I)
CANCEL_RE = re.compile(
    r"\b(?:cancel|cancelled|canceled|delete|remove|invalid|not\s+valid|setup\s+cancel|signal\s+cancel|don't\s+enter|dont\s+enter|"
    r"close\s+order|close\s+pending|usu[nĹ„]|anuluj|anulowane|niewa[zĹĽ]ne)\b",
    re.I,
)
HOLD_RE = re.compile(
    r"\b(?:hold|holding|keep\s+holding|keep\s+hold|still\s+hold|continue\s+holding|"
    r"hold\s+it|hold\s+trade|hold\s+position|trzymaj|trzymamy|dalej\s+trzymaj)\b",
    re.I,
)
PHOENIX_DIRECTION_HINT_RE = re.compile(r"\b(g[oó]r[aę]|d[oó][łl])\b", re.I)
PHOENIX_DIRECTION_RUNNER_ANNOUNCEMENT_RE = re.compile(r"\bwrzuc[eę]\s+teraz\b", re.I)
PHOENIX_NUMERIC_RANGE_RE = re.compile(
    r"^\s*(\d{3,5}(?:[.,]\d+)?)\s*[/\-]\s*(\d{3,5}(?:[.,]\d+)?)\s*$"
)
SECURE_RE = re.compile(
    r"\b(?:secure|secured|secure\s+trade|secure\s+position|set\s+be|set\s+b/e|"
    r"move\s+sl\s+to\s+be|move\s+sl\s+to\s+b/e|"
    r"(?:ustaw|przesu[nń]|przenosimy|dajemy)\w*\s+(?:sl\s+)?(?:na\s+)?(?:be|b/e|breakeven|break\s*even)|"
    r"(?:sl|stop\s*loss)\s+(?:to|na|at)?\s*(?:be|b/e|breakeven|break\s*even)|"
    r"can\s+close|close\s+now|close\s+trade|close\s+position|zabezpiecz|zabezpieczamy|"
    r"zamknij|zamykamy)\b",
    re.I,
)
SECURE_STANDALONE_BE_RE = re.compile(
    r"^\s*(?:be|b/e|breakeven|break\s*even)(?:\s+(?:now|teraz))?[.!✅\s]*$",
    re.I,
)
SECURE_ADVISORY_RE = re.compile(
    r"\b(?:zalecam|polecam|warto|mo[zż]na|radz[eę]|recommend|recommended|suggest|consider)\b",
    re.I,
)
SECURE_IMMEDIATE_RE = re.compile(r"\b(?:teraz|natychmiast|od\s+razu|now|immediately)\b", re.I)
TP_HIT_RE = re.compile(r"\btp\s*([1-9]\d?)\b", re.I)
GOLD_ASSET_RE = re.compile(r"\b(?:xau\s*/?\s*usd|xauusd|gold)\b", re.I)
NAS100_ASSET_RE = re.compile(r"\b(?:nas\s*100|nas100|us100|ustec)\b", re.I)
US30_ASSET_RE = re.compile(r"\b(?:us\s*30|us30|dj\s*30|dj30|dow(?:\s*jones)?|wall\s*street\s*30)\b", re.I)
BTC_ASSET_RE = re.compile(r"\b(?:btc\s*/?\s*usd|btcusd|btc|bitcoin)\b", re.I)
ASSET_SYMBOLS = {
    "gold": "XAUUSD",
    "nas100": "NAS100",
    "us30": "US30",
    "btc": "BTCUSD",
    "ger40": "GER40",
    "wti": "WTI",
}
ASSET_SYMBOL_CANDIDATES = {
    "gold": ("XAUUSD", "GOLD"),
    "nas100": ("NAS100", "US100", "USTEC", "NACUSD.c", "NDAQ"),
    "us30": ("US30", "DJ30", "DJ30ft"),
    "btc": ("BTCUSD", "BTCUSD+", "BTCUSDm"),
    "ger40": ("GER40", "DE40", "DAX40"),
    # PU Prime/Vantage expose WTI cash as USOUSD. AXTIUSD is AXT Inc stock.
    "wti": ("USOUSD", "WTI", "USOIL", "XTIUSD", "CL-OIL"),
    "audjpy": ("AUDJPY",),
    "audusd": ("AUDUSD",),
    "audcad": ("AUDCAD",),
    "audnzd": ("AUDNZD",),
    "audchf": ("AUDCHF",),
    "chfjpy": ("CHFJPY",),
    "euraud": ("EURAUD",),
    "eurcad": ("EURCAD",),
    "eurnzd": ("EURNZD",),
    "eurchf": ("EURCHF",),
    "eurgbp": ("EURGBP",),
    "eurjpy": ("EURJPY",),
    "eurusd": ("EURUSD",),
    "gbpchf": ("GBPCHF",),
    "gbpjpy": ("GBPJPY",),
    "gbpusd": ("GBPUSD",),
    "gbpaud": ("GBPAUD",),
    "gbpcad": ("GBPCAD",),
    "gbpnzd": ("GBPNZD",),
    "cadjpy": ("CADJPY",),
    "cadchf": ("CADCHF",),
    "nzdjpy": ("NZDJPY",),
    "nzdusd": ("NZDUSD",),
    "nzdcad": ("NZDCAD",),
    "nzdchf": ("NZDCHF",),
    "usdcad": ("USDCAD",),
    "usdchf": ("USDCHF",),
    "usdjpy": ("USDJPY",),
}
ASSET_MAX_SPREAD_POINTS = {
    "btc": 3000.0,
}
NEAR_ENTRY_MARKET_TOLERANCE = 4.0
PHOENIX_MARKET_ENTRY_TOLERANCE = 2.0
PHOENIX_MIN_MARKET_TP1_DISTANCE = 0.5
PHOENIX_MAX_LEVEL_MARKET_DISTANCE = 25.0
PHOENIX_BE_BUFFER_USD = 0.10


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "true" if default else "false") or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _is_retryable_market_data_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    return any(
        marker in message
        for marker in (
            "symbol_info_tick failed",
            "market data unavailable",
            "no tick",
        )
    )


def _stale_profit_exit_update(
    managed: dict,
    *,
    side: str,
    current_price: float,
    floating_profit: float,
    now: datetime,
    stale_minutes: float,
    min_profit: float = 0.0,
) -> tuple[bool, bool, int]:
    """Track TP progress and flag a profitable leg that stopped advancing."""
    tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0.0]
    if side not in {"buy", "sell"} or not tps or stale_minutes <= 0.0:
        return False, False, 0

    reached_level = 0
    for index, tp in enumerate(tps, start=1):
        if (side == "buy" and current_price >= tp) or (side == "sell" and current_price <= tp):
            reached_level = index

    previous_level = int(managed.get("live_exit_reached_tp_level", 0) or 0)
    changed = False
    if reached_level > previous_level:
        previous_level = reached_level
        managed["live_exit_reached_tp_level"] = reached_level
        managed["live_exit_last_progress_utc"] = now.isoformat()
        changed = True
        return False, changed, reached_level

    if previous_level <= 0 and managed.get("protected_to_tp1"):
        previous_level = 1
        managed["live_exit_reached_tp_level"] = 1
        managed["live_exit_last_progress_utc"] = str(managed.get("protected_utc", "") or now.isoformat())
        changed = True

    if previous_level <= 0:
        return False, changed, reached_level

    raw_progress = str(managed.get("live_exit_last_progress_utc", "") or "")
    try:
        last_progress = datetime.fromisoformat(raw_progress.replace("Z", "+00:00"))
        if last_progress.tzinfo is None:
            last_progress = last_progress.replace(tzinfo=UTC)
    except Exception:
        managed["live_exit_last_progress_utc"] = now.isoformat()
        return False, True, reached_level

    stale_seconds = max(60.0, float(stale_minutes) * 60.0)
    is_stale = (now.astimezone(UTC) - last_progress.astimezone(UTC)).total_seconds() >= stale_seconds
    return bool(is_stale and float(floating_profit) > float(min_profit)), changed, reached_level


def _should_execute_fresh_edited_signal(
    *,
    is_phoenix: bool,
    is_ghp_gold: bool = False,
    updated_positions: int,
    message_date: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Recover fresh Phoenix/GHP posts whose usable levels arrive as an edit."""
    if updated_positions > 0:
        return False
    if is_phoenix:
        enabled = _env_bool("PHOENIX_EXECUTE_FRESH_EDITED_SIGNALS", True)
        max_age_seconds = max(5.0, _env_float("PHOENIX_FRESH_EDIT_MAX_AGE_SECONDS", 180.0))
    elif is_ghp_gold:
        enabled = _env_bool("GHP_EXECUTE_FRESH_EDITED_SIGNALS", True)
        max_age_seconds = max(5.0, _env_float("GHP_FRESH_EDIT_MAX_AGE_SECONDS", 180.0))
    else:
        return False
    if not enabled or message_date is None:
        return False
    current = now or datetime.now(UTC)
    published = message_date if message_date.tzinfo is not None else message_date.replace(tzinfo=UTC)
    age_seconds = max(0.0, (current.astimezone(UTC) - published.astimezone(UTC)).total_seconds())
    return age_seconds <= max_age_seconds


def _min_hold_seconds() -> float:
    base = max(0.0, _env_float("UPCOMERS_MIN_HOLD_SECONDS", 0.0))
    jitter = max(0.0, _env_float("UPCOMERS_MIN_HOLD_JITTER_SECONDS", 0.0))
    if base <= 0:
        return 0.0
    return max(1.0, base + random.uniform(-jitter, jitter))


def _delay_broker_levels_for_min_hold() -> bool:
    return _min_hold_seconds() > 0 and _env_bool("UPCOMERS_DELAY_BROKER_TP_SL", True)


def _position_age_seconds(position) -> float:
    opened = int(getattr(position, "time", 0) or 0)
    if opened <= 0:
        return 999999.0
    return max(0.0, time.time() - float(opened))


def _hold_remaining_seconds(position, managed: dict) -> float:
    target = float(managed.get("min_hold_seconds", 0.0) or 0.0)
    if target <= 0:
        return 0.0
    return max(0.0, target - _position_age_seconds(position))


def _env_target_tuple(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    values: list[int] = []
    for item in raw.split(","):
        try:
            value = int(item.strip())
        except Exception:
            continue
        if value > 0:
            values.append(value)
    return tuple(values) or default


def _env_int_set(name: str, default: set[int] | None = None) -> set[int]:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return set(default or set())
    values: set[int] = set()
    for item in raw.split(","):
        try:
            values.add(int(item.strip()))
        except Exception:
            continue
    return values or set(default or set())


def _channel_asset_allowed(chat_id: int | None, asset: str) -> bool:
    """Apply optional per-channel asset allowlists without affecting other sources."""
    raw = str(os.getenv("SIGNAL_CHANNEL_ASSET_ALLOWLIST", "") or "").strip()
    if not raw or chat_id is None:
        return True
    try:
        configured = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(configured, dict):
        return True
    allowed = configured.get(str(int(chat_id)))
    if allowed is None:
        return True
    if isinstance(allowed, str):
        allowed = [item.strip() for item in allowed.split(",")]
    if not isinstance(allowed, (list, tuple, set)):
        return True
    normalized = {str(item).strip().lower() for item in allowed if str(item).strip()}
    return str(asset or "").strip().lower() in normalized


def _planned_market_reward_risk(entry: float, sl: float, tp: float) -> float:
    risk = abs(float(entry) - float(sl))
    if risk <= 0.0:
        return 0.0
    return abs(float(tp) - float(entry)) / risk


def _defer_ghp_currency_provider_be(
    chat_id: int | None,
    managed: dict,
    current_price: float,
) -> bool:
    """Ignore an early GHP Currency BE message until TP1 was actually reached."""
    if int(chat_id or 0) != -1003495213392:
        return False
    if not _env_bool("GHP_CURRENCY_DEFER_PROVIDER_BE_UNTIL_TP1", True):
        return False
    if bool(managed.get("protected_to_tp1")):
        return False
    side = str(managed.get("side", "") or "").lower()
    tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0.0]
    if side not in {"buy", "sell"} or not tps:
        return False
    return not _price_reached_tp1_trigger(side, float(current_price), tps[0])
TFXC_MIN_MOMENTUM_TP_DISTANCE = 0.8
TFXC_MAX_ACTIVE_SIGNALS = 1
TFXC_SIGNAL_COOLDOWN_MINUTES = 20.0
XAU_MAX_STOP_DISTANCE = 6.0
NAS100_MAX_STOP_DISTANCE = 80.0
US30_MAX_STOP_DISTANCE = 120.0
BTC_MAX_STOP_DISTANCE = 900.0
MIN_MARKET_TP1_RR = float(os.getenv("SIGNAL_MIN_MARKET_TP1_RR", "0.55") or 0.55)


def _masked(value: object, prefix: int = 2, suffix: int = 2) -> str:
    text = str(value or "").strip()
    if not text:
        return "hidden"
    if len(text) <= prefix + suffix:
        return "*" * len(text)
    return f"{text[:prefix]}{'*' * max(4, len(text) - prefix - suffix)}{text[-suffix:]}"


@dataclass
class ParsedSignal:
    uid: str
    chat_id: int | None
    chat_title: str
    post_author: str
    message_id: int
    side: str
    asset: str
    entry: float
    entries: list[float]
    sl: float
    tp: float
    tps: list[float]
    order_type: int
    order_kind: str
    raw_text: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChannelStrategy:
    name: str
    target_index: int
    protect_mode: str = "none"
    atr_mult: float = 0.0
    force_all_entries: bool = False
    limit_only_ranges: bool = False
    split_target_indices: tuple[int, ...] = ()
    strict_market_tolerance: float = 0.0
    min_market_tp1_distance: float = 0.0
    allow_market_runner: bool = True
    pending_expiry_minutes: float = 0.0
    split_protect_modes: tuple[str, ...] = ()
    entry_policy: str = "market_between_entry_tp1"
    max_stop_distance: float = 0.0


DEFAULT_STRATEGY = ChannelStrategy(
    "funded_goal_market_before_tp1_tp1_tp1_tp2_be",
    2,
    "be",
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.5,
    pending_expiry_minutes=15.0,
)
RANGE_STRATEGY = ChannelStrategy(
    "funded_goal_range_market_before_tp1_tp1_tp1_tp2_be",
    2,
    "be",
    force_all_entries=False,
    limit_only_ranges=False,
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.5,
    pending_expiry_minutes=15.0,
)
SAFE_TP1_TP2_BE_STRATEGY = ChannelStrategy(
    "funded_goal_market_before_tp1_tp1_tp1_tp2_be",
    1,
    "be",
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.5,
    pending_expiry_minutes=15.0,
)
HIGH_RATE_TP1_TP2_BE_STRATEGY = ChannelStrategy(
    "new_high_rate_market_before_tp1_tp1_tp1_tp2_be",
    2,
    "be",
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.0,
    pending_expiry_minutes=15.0,
)
DISCOVERED_HIGH_RATE_STRATEGY = ChannelStrategy(
    "discovered_high_rate_tp1_tp1_tp2_be_fast_expiry",
    2,
    "be",
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=1.5,
    min_market_tp1_distance=1.0,
    pending_expiry_minutes=10.0,
)
DISCOVERED_DEFENSIVE_TP1_STRATEGY = ChannelStrategy(
    "discovered_defensive_tp1_tp1_be_fast_expiry",
    1,
    "be",
    split_target_indices=(1, 1),
    strict_market_tolerance=1.5,
    min_market_tp1_distance=0.8,
    pending_expiry_minutes=8.0,
)
HIGH_RATE_TP1_ONLY_STRATEGY = ChannelStrategy(
    "new_high_rate_tp1_scalp_be",
    1,
    "be",
    split_target_indices=(1, 1),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.0,
    pending_expiry_minutes=15.0,
)
FAST_TP_HIT_CANCEL_STRATEGY = ChannelStrategy(
    "fast_tp_hit_cancel_tp1_tp1_tp2_be",
    2,
    "be",
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.5,
    pending_expiry_minutes=_env_float("FAST_TP_HIT_PENDING_EXPIRY_MINUTES", 6.0),
)
SAFE_ZONE_TP1_TP2_BE_STRATEGY = ChannelStrategy(
    "funded_goal_zone_market_before_tp1_tp1_tp1_tp2_be",
    2,
    "be",
    force_all_entries=False,
    limit_only_ranges=False,
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.5,
    pending_expiry_minutes=15.0,
)
GOLD_BTC_XAUUSD_ZONE_TEST_STRATEGY = ChannelStrategy(
    "gold_btc_xauusd_zone_test_3_limits_tp1_tp2_tp3_be_no_chase",
    3,
    "be",
    force_all_entries=True,
    limit_only_ranges=True,
    split_target_indices=(1, 2, 3),
    strict_market_tolerance=0.0,
    min_market_tp1_distance=2.0,
    allow_market_runner=False,
    pending_expiry_minutes=15.0,
)
NAS_SAFE_TP1_TP2_BE_STRATEGY = ChannelStrategy(
    "nas100_us30_funded_goal_tp1_tp1_tp2_be",
    2,
    "be",
    split_target_indices=(1, 1, 2),
    strict_market_tolerance=25.0,
    min_market_tp1_distance=15.0,
    pending_expiry_minutes=15.0,
)
PHOENIX_STRATEGY = ChannelStrategy(
    "phoenix_zone_tp1_tp2_tp4_signal_sl_delayed_be",
    4,
    "be_after_tp3",
    1.2,
    force_all_entries=True,
    limit_only_ranges=True,
    split_target_indices=_env_target_tuple("PHOENIX_SPLIT_TARGET_INDICES", (1, 2, 4)),
    strict_market_tolerance=_env_float("PHOENIX_MARKET_ENTRY_TOLERANCE", PHOENIX_MARKET_ENTRY_TOLERANCE),
    min_market_tp1_distance=_env_float("PHOENIX_MIN_MARKET_TP1_DISTANCE", PHOENIX_MIN_MARKET_TP1_DISTANCE),
    allow_market_runner=True,
    pending_expiry_minutes=_env_float("PHOENIX_PENDING_EXPIRY_MINUTES", 15.0),
    split_protect_modes=("none", "be_after_tp3", "be_after_tp3"),
    entry_policy="market_between_entry_tp1",
    max_stop_distance=0.0,
)

FXGOLD_TP1_STRATEGY = ChannelStrategy(
    "fxgold_100d_tp1_all_no_be_wide_market",
    1,
    split_target_indices=(1, 1, 1),
    split_protect_modes=("none", "none", "none"),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.0,
    pending_expiry_minutes=15.0,
    entry_policy="market_between_entry_tp1",
    max_stop_distance=6.0,
)
XAU_PIPS_TP1_STRATEGY = ChannelStrategy(
    "xau_pips_100d_tp1_all_no_be_wide_market",
    1,
    split_target_indices=(1, 1, 1),
    split_protect_modes=("none", "none", "none"),
    strict_market_tolerance=2.0,
    min_market_tp1_distance=1.0,
    pending_expiry_minutes=15.0,
    entry_policy="market_between_entry_tp1",
    max_stop_distance=6.0,
)
GOLDPRO_WIDE_PENDING_STRATEGY = ChannelStrategy(
    "goldpro_100d_tp1_tp2_tp3_no_be_wide_pending",
    3,
    split_target_indices=(1, 2, 3),
    split_protect_modes=("none", "none", "none"),
    allow_market_runner=False,
    pending_expiry_minutes=15.0,
    entry_policy="nearest_pending_15m",
    max_stop_distance=6.0,
)
GHP_GOLD_DEEP_BE_STRATEGY = ChannelStrategy(
    "ghp_gold_tp1_tp2_deep_original_sl_be_60m",
    99,
    "be",
    force_all_entries=True,
    split_target_indices=(1, 2, 99),
    split_protect_modes=("none", "be", "be"),
    allow_market_runner=False,
    pending_expiry_minutes=_env_float("GHP_GOLD_PENDING_EXPIRY_MINUTES", 60.0),
    entry_policy="market_between_entry_tp1",
)
GHP_TP1_ORIGINAL_SL_STRATEGY = ChannelStrategy(
    "ghp_tp1_original_sl_90s",
    1,
    "none",
    split_target_indices=(1,),
    split_protect_modes=("none",),
    pending_expiry_minutes=60.0,
    entry_policy="market_between_entry_tp1",
)
GHP_CURRENCY_TP1_TP2_STRATEGY = ChannelStrategy(
    "ghp_currency_tp1_original_sl_60m",
    1,
    "none",
    split_target_indices=(1,),
    split_protect_modes=("none",),
    pending_expiry_minutes=60.0,
    entry_policy="market_between_entry_tp1",
)
THREE_LEG_TARGET_PLAN = (
    (1, "be"),
    (1, "be"),
    (2, "be"),
)
CHANNEL_STRATEGIES: dict[str, ChannelStrategy] = {
    "-1001220837618": ChannelStrategy(
        "tfxc_low_lot_tp1_tp1_be",
        1,
        "be",
        split_target_indices=(1, 1),
        strict_market_tolerance=2.0,
        min_market_tp1_distance=0.8,
        pending_expiry_minutes=15.0,
    ),
    "1001220837618": ChannelStrategy(
        "tfxc_low_lot_tp1_tp1_be",
        1,
        "be",
        split_target_indices=(1, 1),
        strict_market_tolerance=2.0,
        min_market_tp1_distance=0.8,
        pending_expiry_minutes=15.0,
    ),
    "tfxc premium": ChannelStrategy(
        "tfxc_low_lot_tp1_tp1_be",
        1,
        "be",
        split_target_indices=(1, 1),
        strict_market_tolerance=2.0,
        min_market_tp1_distance=0.8,
        pending_expiry_minutes=15.0,
    ),
    "fxgoldxauusdfree": DISCOVERED_HIGH_RATE_STRATEGY,
    "fx gold xauusd free signals": DISCOVERED_HIGH_RATE_STRATEGY,
    "fx gold xauusd": FAST_TP_HIT_CANCEL_STRATEGY,
    "saeal": FAST_TP_HIT_CANCEL_STRATEGY,
    "gold_btc_xauusd_forex_vip_sign": GOLD_BTC_XAUUSD_ZONE_TEST_STRATEGY,
    "gold_btcusd_xauusd": GOLD_BTC_XAUUSD_ZONE_TEST_STRATEGY,
    "goldinsighthub526": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "nas100_xauusdsignals": HIGH_RATE_TP1_TP2_BE_STRATEGY,
    "forexgoldgbpkiller": DISCOVERED_HIGH_RATE_STRATEGY,
    "forex gold gbp killer": DISCOVERED_HIGH_RATE_STRATEGY,
    "forex_best_gold_vip_signals": DISCOVERED_HIGH_RATE_STRATEGY,
    "fx- gold ( traders )": DISCOVERED_HIGH_RATE_STRATEGY,
    "forex_gold_daily_free_signals": DISCOVERED_HIGH_RATE_STRATEGY,
    "forex gold signals (free)": DISCOVERED_HIGH_RATE_STRATEGY,
    "forex_gold_pro_traders": DISCOVERED_HIGH_RATE_STRATEGY,
    "gold pro trader (xauusd)": DISCOVERED_HIGH_RATE_STRATEGY,
    "xauusd_gold_killer_vip": DISCOVERED_HIGH_RATE_STRATEGY,
    "xauusd gold killer (vip)": DISCOVERED_HIGH_RATE_STRATEGY,
    "gold_hunter_paul_signals": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "goldhunter paul  fx world wide": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "gold_pro_forex_trader_signal": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "gold pro forex trader": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "-1003576763534": DISCOVERED_HIGH_RATE_STRATEGY,
    "-1003825897091": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "-1002089036784": DISCOVERED_HIGH_RATE_STRATEGY,
    "-1003669904323": DISCOVERED_HIGH_RATE_STRATEGY,
    "-1003547531627": DISCOVERED_HIGH_RATE_STRATEGY,
    "-1003212344580": DISCOVERED_HIGH_RATE_STRATEGY,
    "-1001826528649": GOLD_BTC_XAUUSD_ZONE_TEST_STRATEGY,
    "-1003991839723": GOLD_BTC_XAUUSD_ZONE_TEST_STRATEGY,
    "-1003607413338": DISCOVERED_HIGH_RATE_STRATEGY,
    "-1003965834944": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "-1001866926040": DISCOVERED_DEFENSIVE_TP1_STRATEGY,
    "goldxauusdsignale1": HIGH_RATE_TP1_ONLY_STRATEGY,
    "-1002864291293": PHOENIX_STRATEGY,
    "1002864291293": PHOENIX_STRATEGY,
    "phoenix vip": PHOENIX_STRATEGY,
    "-1001515052582": SAFE_TP1_TP2_BE_STRATEGY,
    "1001515052582": SAFE_TP1_TP2_BE_STRATEGY,
    "forex_money_btc_siganls1111": SAFE_TP1_TP2_BE_STRATEGY,
    "xauusd (gold) trader": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001819344275": FAST_TP_HIT_CANCEL_STRATEGY,
    "1001819344275": FAST_TP_HIT_CANCEL_STRATEGY,
    "trade_with_saeal": FAST_TP_HIT_CANCEL_STRATEGY,
    "trade with saeal": FAST_TP_HIT_CANCEL_STRATEGY,
    "-1002528249483": SAFE_TP1_TP2_BE_STRATEGY,
    "1002528249483": SAFE_TP1_TP2_BE_STRATEGY,
    "ghptrading": GHP_GOLD_DEEP_BE_STRATEGY,
    "-1002033681012": GHP_GOLD_DEEP_BE_STRATEGY,
    "1002033681012": GHP_GOLD_DEEP_BE_STRATEGY,
    "-1001958009741": GHP_GOLD_DEEP_BE_STRATEGY,
    "1001958009741": GHP_GOLD_DEEP_BE_STRATEGY,
    "-1003306025363": GHP_TP1_ORIGINAL_SL_STRATEGY,
    "1003306025363": GHP_TP1_ORIGINAL_SL_STRATEGY,
    "-1003495213392": GHP_CURRENCY_TP1_TP2_STRATEGY,
    "1003495213392": GHP_CURRENCY_TP1_TP2_STRATEGY,
    "-1001602855991": SAFE_TP1_TP2_BE_STRATEGY,
    "1001602855991": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001914224843": SAFE_TP1_TP2_BE_STRATEGY,
    "1001914224843": SAFE_TP1_TP2_BE_STRATEGY,
    "royal_gold_signals": ChannelStrategy("fixed_tp4", 4, "none"),
    "royal gold signals": ChannelStrategy("fixed_tp4", 4, "none"),
    "-1001704634655": SAFE_TP1_TP2_BE_STRATEGY,
    "1001704634655": SAFE_TP1_TP2_BE_STRATEGY,
    "xauusd_pips_killers": SAFE_TP1_TP2_BE_STRATEGY,
    "xauusd pips killer": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001150362511": SAFE_TP1_TP2_BE_STRATEGY,
    "1001150362511": SAFE_TP1_TP2_BE_STRATEGY,
    "fx_gold_killler": SAFE_TP1_TP2_BE_STRATEGY,
    "fx_gld_killer": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001365880004": SAFE_TP1_TP2_BE_STRATEGY,
    "1001365880004": SAFE_TP1_TP2_BE_STRATEGY,
    "wolfxsignals_freegroup": SAFE_TP1_TP2_BE_STRATEGY,
    "wolfx signals": SAFE_TP1_TP2_BE_STRATEGY,
    "trade_with_mthri": SAFE_ZONE_TP1_TP2_BE_STRATEGY,
    "trade with mthri": SAFE_ZONE_TP1_TP2_BE_STRATEGY,
    "-1001838220681": SAFE_TP1_TP2_BE_STRATEGY,
    "1001838220681": SAFE_TP1_TP2_BE_STRATEGY,
    "goldprotradertame": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001710435979": SAFE_TP1_TP2_BE_STRATEGY,
    "1001710435979": SAFE_TP1_TP2_BE_STRATEGY,
    "gold pro trader": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001774783341": SAFE_TP1_TP2_BE_STRATEGY,
    "1001774783341": SAFE_TP1_TP2_BE_STRATEGY,
    "gary_thetrader": SAFE_TP1_TP2_BE_STRATEGY,
    "gary gold trader": SAFE_TP1_TP2_BE_STRATEGY,
    "-1001232813229": NAS_SAFE_TP1_TP2_BE_STRATEGY,
    "1001232813229": NAS_SAFE_TP1_TP2_BE_STRATEGY,
    "vipnas100 pro": NAS_SAFE_TP1_TP2_BE_STRATEGY,
    "tagsignals": ChannelStrategy(
        "tagsignals_wide_zone_3_limits_tp1_tp2_tp3_be",
        3,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 3),
    ),
    "free tag signals": ChannelStrategy(
        "tagsignals_wide_zone_3_limits_tp1_tp2_tp3_be",
        3,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 3),
    ),
    "-1002717527369": ChannelStrategy(
        "tagsignals_wide_zone_3_limits_tp1_tp2_tp3_be",
        3,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 3),
    ),
    "1002717527369": ChannelStrategy(
        "tagsignals_wide_zone_3_limits_tp1_tp2_tp3_be",
        3,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 3),
    ),
    "-1003772508199": ChannelStrategy(
        "gold_signal_provide_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
    "1003772508199": ChannelStrategy(
        "gold_signal_provide_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
    "fffdos": ChannelStrategy(
        "gold_signal_provide_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
    "gold signal provide": ChannelStrategy(
        "gold_signal_provide_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
    "-1001982510222": ChannelStrategy(
        "blue_pips_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
    "1001982510222": ChannelStrategy(
        "blue_pips_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
    "blue pip": ChannelStrategy(
        "blue_pips_zone_3_limits_tp1_tp2_tp4_be",
        4,
        "be",
        force_all_entries=True,
        limit_only_ranges=True,
        split_target_indices=(1, 2, 4),
    ),
}

CHANNEL_STRATEGIES.update(
    {
        "-1003576763534": FXGOLD_TP1_STRATEGY,
        "-1001819344275": ChannelStrategy(
            "saeal_60d_tp1_tp3_tp5_no_be_tight_pending",
            5,
            split_target_indices=(1, 3, 5),
            split_protect_modes=("none", "none", "none"),
            allow_market_runner=False,
            pending_expiry_minutes=15.0,
            entry_policy="nearest_pending_15m",
            max_stop_distance=2.5,
        ),
        "-1003825897091": ChannelStrategy(
            "ictgreen_60d_tp1_tp3_tp5_be_after_tp1_medium_pending",
            5,
            split_target_indices=(1, 3, 5),
            split_protect_modes=("none", "be", "be"),
            allow_market_runner=False,
            pending_expiry_minutes=15.0,
            entry_policy="nearest_pending_15m",
            max_stop_distance=4.0,
        ),
        "-1003669904323": ChannelStrategy(
            "fxgoldtraders_60d_tp1_tp2_tp3_be_after_tp1_wide_pending",
            3,
            split_target_indices=(1, 2, 3),
            split_protect_modes=("none", "be", "be"),
            allow_market_runner=False,
            pending_expiry_minutes=15.0,
            entry_policy="nearest_pending_15m",
            max_stop_distance=6.0,
        ),
        "-1001826528649": ChannelStrategy(
            "goldbtcxauvip_100d_tp1_tp1_tp2_no_be_wide_pending",
            2,
            split_target_indices=(1, 1, 2),
            split_protect_modes=("none", "none", "none"),
            allow_market_runner=False,
            pending_expiry_minutes=15.0,
            entry_policy="nearest_pending_15m",
            max_stop_distance=6.0,
        ),
        "-1001996608301": XAU_PIPS_TP1_STRATEGY,
        "-1003212344580": GOLDPRO_WIDE_PENDING_STRATEGY,
        "-1003965834944": ChannelStrategy(
            "goldhunterworld_60d_tp1_all_no_be_tight_pending",
            1,
            split_target_indices=(1, 1, 1),
            split_protect_modes=("none", "none", "none"),
            allow_market_runner=False,
            pending_expiry_minutes=15.0,
            entry_policy="nearest_pending_15m",
            max_stop_distance=2.5,
        ),
        "-1001199704544": ChannelStrategy(
            "pipsmakers_60d_tp1_tp1_tp2_be_after_tp1_wide_pending",
            2,
            split_target_indices=(1, 1, 2),
            split_protect_modes=("none", "be", "be"),
            allow_market_runner=False,
            pending_expiry_minutes=15.0,
            entry_policy="nearest_pending_15m",
            max_stop_distance=6.0,
        ),
        "-1001232813229": ChannelStrategy(
            "vipnas100_60d_tp1_tp1_tp2_be_after_tp1_wide_market",
            2,
            split_target_indices=(1, 1, 2),
            split_protect_modes=("none", "be", "be"),
            pending_expiry_minutes=15.0,
            entry_policy="market_between_entry_tp1",
            max_stop_distance=100.0,
        ),
    }
)


def _channel_strategy(signal: ParsedSignal) -> ChannelStrategy:
    if _is_dany_signals_source(signal.chat_id, signal.chat_title):
        normalized = _normalize_text(signal.raw_text).lower()
        if "material ma charakter edukacyjny" in normalized and len(signal.entries) > 1:
            return PHOENIX_STRATEGY
        if signal.asset == "gold":
            return GHP_GOLD_DEEP_BE_STRATEGY
        if signal.asset in {"nas100", "us30", "ger40", "btc", "wti"}:
            return GHP_TP1_ORIGINAL_SL_STRATEGY
        return GHP_CURRENCY_TP1_TP2_STRATEGY
    candidates = [
        str(signal.chat_id or "").lower(),
        str(abs(signal.chat_id or 0)).lower() if signal.chat_id else "",
        str(signal.chat_title or "").strip().lower(),
    ]
    for key in candidates:
        if key in CHANNEL_STRATEGIES:
            return CHANNEL_STRATEGIES[key]
    title = str(signal.chat_title or "").strip().lower()
    for key, strategy in CHANNEL_STRATEGIES.items():
        if key and key in title:
            return strategy
    if len(signal.entries) > 1:
        return RANGE_STRATEGY
    return DEFAULT_STRATEGY


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict) -> None:
    event = {"timestamp_utc": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"processed": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("processed", [])
            return raw
    except Exception:
        pass
    return {"processed": []}


def _normalize_watch_targets(items: tuple[str, ...]) -> tuple[set[int], set[int], set[str]]:
    raw_ids: set[int] = set()
    abs_ids: set[int] = set()
    usernames: set[str] = set()
    for item in items:
        token = item.strip()
        if not token:
            continue
        if token.startswith("@"):
            usernames.add(token[1:].lower())
            continue
        try:
            value = int(token)
            raw_ids.add(value)
            abs_ids.add(abs(value))
        except Exception:
            usernames.add(token.lower())
    return raw_ids, abs_ids, usernames


def _channel_key_variants(value: object) -> set[str]:
    token = str(value or "").strip().strip("'\"").lower()
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if token.startswith(prefix):
            token = token[len(prefix) :].strip("/")
            break
    token = token.lstrip("@").strip("/")
    if "/" in token and not token.startswith("+"):
        token = token.split("/", 1)[0].strip()
    variants = {token} if token else set()
    try:
        numeric = str(abs(int(token)))
        variants.add(numeric)
        variants.add(str(int(token)))
        if numeric.startswith("100") and len(numeric) > 3:
            variants.add(numeric[3:])
        else:
            variants.add(f"100{numeric}")
            variants.add(f"-100{numeric}")
    except Exception:
        pass
    return {item for item in variants if item}


def _channel_lot_override(cfg, signal: ParsedSignal, channel_username: str = "") -> float | None:
    candidates: set[str] = set()
    for value in (signal.chat_id, signal.chat_title, channel_username):
        candidates.update(_channel_key_variants(value))
    for configured_channel, raw_lot in getattr(cfg, "channel_lot_sizes", {}).items():
        if not (_channel_key_variants(configured_channel) & candidates):
            continue
        try:
            lot = float(raw_lot)
        except Exception:
            return None
        return lot if lot > 0 else None
    return None


def _save_state(path: Path, state: dict) -> None:
    processed = list(dict.fromkeys(state.get("processed", [])))[-1000:]
    state = {**state, "processed": processed}
    _write_json(path, state)


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\n", " ").split()).strip()


def _extract_side(text: str) -> str | None:
    match = SIDE_RE.search(text)
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"buy", "buying", "long", "compra"}:
        return "buy"
    if token in {"sell", "selling", "short", "venta"}:
        return "sell"
    return None


def _extract_order_kind(text: str) -> str:
    match = ORDER_KIND_RE.search(text)
    if not match:
        return "market"
    token = (match.group(1) or match.group(2) or "market").lower()
    if token in {"limite", "lĂ­mite"}:
        return "limit"
    return token


def _valid_gold_price(value: float) -> bool:
    return value >= MIN_GOLD_PRICE


def _line_prices(text: str, *, allow_relative_pips: bool = False) -> list[float]:
    prices: list[float] = []
    for match in PRICE_VALUE_RE.finditer(text):
        try:
            value = float(match.group(0))
        except Exception:
            continue
        if allow_relative_pips or _valid_gold_price(value):
            prices.append(value)
    return prices


def _extract_tp_values(text: str) -> list[float]:
    values: list[float] = []
    collect_following_prices = False
    for line in text.splitlines():
        label_matches = list(TP_LABEL_RE.finditer(line))
        if not label_matches:
            if collect_following_prices:
                if re.search(r"\b(?:entry|entries|enter|open|price|zone|buyzone|sellzone|sl|s/l|stop\s*loss|buy|buying|sell|selling|long|short)\b", line, re.I):
                    collect_following_prices = False
                    continue
                prices = _line_prices(line)
                if prices:
                    values.extend(prices)
                    continue
                if line.strip():
                    collect_following_prices = False
            continue
        labeled_prices: list[float] = []
        for index, match in enumerate(label_matches):
            segment_end = label_matches[index + 1].start() if index + 1 < len(label_matches) else len(line)
            segment = line[match.end() : segment_end]
            segment = re.split(r"\b(?:entry|entries|enter|open|zone|sl|s/l|stop\s*loss)\b", segment, maxsplit=1, flags=re.I)[0]
            segment_prices = _line_prices(segment)
            if segment_prices:
                if len(label_matches) == 1:
                    labeled_prices.extend(float(value) for value in segment_prices)
                else:
                    labeled_prices.append(float(segment_prices[0]))
        if not labeled_prices and RELATIVE_PIPS_RE.search(line):
            continue
        if labeled_prices:
            values.extend(labeled_prices)
            collect_following_prices = False
        else:
            collect_following_prices = True
    return values


def _extract_float(regex: re.Pattern[str], text: str) -> float | None:
    match = regex.search(text)
    if not match:
        return None
    for group in reversed(match.groups()):
        if not group:
            continue
        try:
            value = float(group)
        except Exception:
            continue
        if _valid_gold_price(value):
            return value
    return None


def _extract_entry_range(text: str, *, allow_bare: bool = False, allow_side_bare: bool = False) -> tuple[float, float] | None:
    match = ENTRY_RANGE_RE.search(text)
    if not match and allow_side_bare:
        match = SIDE_BARE_RANGE_RE.search(text)
    if not match and allow_side_bare:
        match = SIDE_DOT_RANGE_RE.search(text)
    if not match and allow_side_bare:
        match = SIDE_SPACE_RANGE_RE.search(text)
    if not match and allow_bare:
        match = BARE_ENTRY_RANGE_RE.search(text)
    if not match:
        return None
    try:
        a = float(match.group(1))
        b = float(match.group(2))
    except Exception:
        return None
    if not _valid_gold_price(a) or not _valid_gold_price(b):
        return None
    return a, b


def _is_tagsignals_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return int(chat_id or 0) == -1002717527369 or "tag signals" in title or "tagsignals" in title


def _is_tfxc_premium_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return int(chat_id or 0) == -1001220837618 or "tfxc premium" in title


def _is_xauusd_gold_signal_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return int(chat_id or 0) == -1001914224843 or "xauusd gold signal" in title


def _is_phoenix_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return int(chat_id or 0) == -1002864291293 or "phoenix" in title


def _is_dany_signals_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return int(chat_id or 0) == -1004410781005 or title == "dany signals"


def _normalize_dany_signal_targets(text: str) -> str:
    """Label bare target-price lines used by the curated relay."""
    if TP_LABEL_RE.search(text) or not SIDE_RE.search(text) or not SL_RE.search(text):
        return text
    lines = str(text or "").splitlines()
    saw_sl = False
    target_count = 0
    normalized: list[str] = []
    for line in lines:
        if SL_RE.search(line):
            saw_sl = True
            normalized.append(line)
            continue
        stripped = line.strip()
        if saw_sl and re.fullmatch(r"\d{3,6}(?:\.\d+)?", stripped):
            target_count += 1
            normalized.append(f"TP{target_count} {stripped}")
            continue
        normalized.append(line)
    return "\n".join(normalized) if target_count else text


def _managed_source_message_id(managed: dict) -> int:
    uid = str(managed.get("signal_uid", "") or "")
    parts = uid.split(":", 2)
    if len(parts) >= 2:
        try:
            return int(parts[1] or 0)
        except (TypeError, ValueError):
            pass
    try:
        return int(managed.get("message_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _managed_source_message_ids(managed: dict) -> set[int]:
    message_ids = {_managed_source_message_id(managed)}
    for value in managed.get("provider_revision_message_ids", []) or []:
        try:
            message_ids.add(int(value or 0))
        except (TypeError, ValueError):
            continue
    return {message_id for message_id in message_ids if message_id > 0}


def _channel_update_target_message_ids(
    managed_rows: list[dict],
    chat_id: int | None,
    chat_title: str,
    update_message_id: int,
    reply_to_message_id: int = 0,
) -> set[int]:
    """Route a channel update to one signal instead of every live signal in that chat."""
    normalized_title = str(chat_title or "").strip().lower()
    candidate_ids: set[int] = set()
    for managed in managed_rows:
        managed_chat_id = int(managed.get("chat_id", 0) or 0)
        managed_title = str(managed.get("chat_title", "") or "").strip().lower()
        same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
        same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
        if not (same_chat or same_title):
            continue
        candidate_ids.update(_managed_source_message_ids(managed))

    if not candidate_ids:
        return set()
    reply_id = int(reply_to_message_id or 0)
    if reply_id > 0:
        return {reply_id} if reply_id in candidate_ids else set()
    update_id = int(update_message_id or 0)
    if update_id in candidate_ids:
        return {update_id}
    preceding = [message_id for message_id in candidate_ids if update_id <= 0 or message_id < update_id]
    return {max(preceding or candidate_ids)}


def _telegram_reply_to_message_id(message) -> int:
    try:
        direct = int(getattr(message, "reply_to_msg_id", 0) or 0)
    except (TypeError, ValueError):
        direct = 0
    if direct > 0:
        return direct
    reply = getattr(message, "reply_to", None)
    try:
        return int(getattr(reply, "reply_to_msg_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_mthri_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return "mthri" in title


def _is_gold_signal_provide_source(chat_id: int | None, chat_title: str) -> bool:
    title = str(chat_title or "").strip().lower()
    return int(chat_id or 0) == -1003772508199 or "gold signal provide" in title or "fffdos" in title


def _gold_levels_sane(asset: str, entry: float | None, entries: list[float], sl: float | None, tps: list[float]) -> bool:
    if asset != "gold":
        return True
    levels = [float(value) for value in ([entry] if entry else []) + entries + ([sl] if sl else []) + tps]
    return all(MIN_GOLD_PRICE <= value <= MAX_GOLD_PRICE for value in levels)


def _levels_from_side_zone(side: str, entry_range: tuple[float, float], text: str) -> tuple[list[float], float]:
    low, high = min(entry_range), max(entry_range)
    prices = []
    for raw in PRICE_VALUE_RE.findall(text):
        try:
            price = float(raw)
        except Exception:
            continue
        if abs(price - low) < 0.001 or abs(price - high) < 0.001:
            continue
        if _valid_gold_price(price):
            prices.append(price)
    if side == "buy":
        tps = sorted({price for price in prices if price > high})
        sl_candidates = sorted(price for price in prices if price < low)
        sl = float(sl_candidates[-1]) if sl_candidates else 0.0
    else:
        tps = sorted({price for price in prices if price < low}, reverse=True)
        sl_candidates = sorted(price for price in prices if price > high)
        sl = float(sl_candidates[0]) if sl_candidates else 0.0
    return [float(value) for value in tps], sl


def _synthetic_zone_tps(side: str, entry_range: tuple[float, float]) -> list[float]:
    low, high = min(entry_range), max(entry_range)
    width = max(high - low, 4.0)
    if side == "buy":
        anchor = high
        return [round(anchor + width * mult, 2) for mult in (0.5, 1.0, 1.5)]
    anchor = low
    return [round(anchor - width * mult, 2) for mult in (0.5, 1.0, 1.5)]


def _synthetic_zone_sl(side: str, entry_range: tuple[float, float]) -> float:
    low, high = min(entry_range), max(entry_range)
    width = max(high - low, 4.0)
    if side == "buy":
        return round(low - width * 0.5, 2)
    return round(high + width * 0.5, 2)


def _build_entry_prices(entry: float | None, entry_range: tuple[float, float] | None) -> list[float]:
    if entry_range:
        low, high = min(entry_range), max(entry_range)
        avg = round((low + high) / 2.0, 3)
        return list(dict.fromkeys([float(low), float(avg), float(high)]))
    if entry and entry > 0:
        return [float(entry)]
    return []


def _repair_phoenix_sl_digit_typo(side: str, entries: list[float], sl: float | None) -> float | None:
    if side not in {"buy", "sell"} or not entries or sl is None or sl <= 0:
        return sl
    low, high = min(entries), max(entries)
    correctly_placed = sl < low if side == "buy" else sl > high
    if correctly_placed and MIN_GOLD_PRICE <= sl <= MAX_GOLD_PRICE:
        return sl

    raw_digits = re.sub(r"\D", "", f"{float(sl):.8f}".rstrip("0").rstrip("."))
    candidates: set[float] = set()
    if len(raw_digits) >= 5:
        for index in range(len(raw_digits)):
            shortened = raw_digits[:index] + raw_digits[index + 1 :]
            if shortened:
                candidates.add(float(shortened))
    candidates.update({float(sl) / 10.0, float(sl) / 100.0})
    valid = []
    for candidate in candidates:
        if not MIN_GOLD_PRICE <= candidate <= MAX_GOLD_PRICE:
            continue
        distance = low - candidate if side == "buy" else candidate - high
        if 0.25 <= distance <= 50.0:
            valid.append((distance, candidate))
    if not valid:
        return sl
    valid.sort(key=lambda item: (item[0], item[1]))
    return float(valid[0][1])


def _infer_side_from_levels(entry: float | None, sl: float | None, tps: list[float], entry_range: tuple[float, float] | None = None) -> str | None:
    if (entry is None or entry <= 0) and not entry_range:
        return None
    if not tps:
        return None
    if entry_range:
        entry_low, entry_high = min(entry_range), max(entry_range)
    else:
        entry_low = entry_high = float(entry or 0.0)
    tp1 = float(tps[0])
    sl_value = float(sl or 0.0)
    if tp1 > entry_high and (sl_value <= 0 or sl_value < entry_low):
        return "buy"
    if tp1 < entry_low and (sl_value <= 0 or sl_value > entry_high):
        return "sell"
    tp_above = sum(1 for tp in tps if float(tp) > entry_high)
    tp_below = sum(1 for tp in tps if float(tp) < entry_low)
    if tp_above > tp_below and (sl_value <= 0 or sl_value < entry_low):
        return "buy"
    if tp_below > tp_above and (sl_value <= 0 or sl_value > entry_high):
        return "sell"
    return None


def _phoenix_direction_hint(text: str) -> str | None:
    match = PHOENIX_DIRECTION_HINT_RE.search(_normalize_text(text or ""))
    if not match:
        return None
    word = match.group(1).lower()
    return "buy" if word.startswith("g") else "sell"


def _is_phoenix_direction_runner_announcement(text: str) -> bool:
    normalized = _normalize_text(text or "")
    return bool(PHOENIX_DIRECTION_RUNNER_ANNOUNCEMENT_RE.search(normalized) and _phoenix_direction_hint(normalized))


def _phoenix_direction_pullback_confirmed(side: str, closes: list[float]) -> bool:
    """Confirm a Phoenix pre-signal when its EMA trend contains a short M1 pullback."""
    values = [float(value) for value in closes if float(value or 0.0) > 0.0]
    if side not in {"buy", "sell"} or len(values) < 22:
        return False

    def ema_last(span: int) -> float:
        alpha = 2.0 / (float(span) + 1.0)
        current = values[0]
        for value in values[1:]:
            current = alpha * value + (1.0 - alpha) * current
        return current

    direction = 1.0 if side == "buy" else -1.0
    trend_aligned = direction * (ema_last(9) - ema_last(21)) > 0.0
    short_momentum = values[-1] - values[-4]
    pullback_present = direction * short_momentum < 0.0
    return bool(trend_aligned and pullback_present)


def _phoenix_numeric_range(text: str) -> list[float]:
    match = PHOENIX_NUMERIC_RANGE_RE.match(str(text or "").strip())
    if not match:
        return []
    values = sorted(float(value.replace(",", ".")) for value in match.groups())
    if values[0] < MIN_GOLD_PRICE or values[1] > MAX_GOLD_PRICE:
        return []
    return values


def _normalize_tps_for_side(side: str, entry: float | None, tps: list[float]) -> list[float]:
    entry_value = float(entry or 0.0)
    if entry_value <= 0:
        return [float(value) for value in tps]
    if side == "buy":
        valid = sorted(float(value) for value in tps if float(value) > entry_value)
    else:
        valid = sorted((float(value) for value in tps if float(value) < entry_value), reverse=True)
    return valid or [float(value) for value in tps]


def _normalize_gold_provider_quote_basis(signal: ParsedSignal, market_price: float) -> tuple[ParsedSignal, float]:
    """Translate market-signal levels when the provider uses a different XAU quote basis."""
    if (
        signal.asset != "gold"
        or signal.order_kind != "market"
        or _is_phoenix_source(signal.chat_id, signal.chat_title)
        or is_ghp_source(signal.chat_id, signal.chat_title)
        or _is_dany_signals_source(signal.chat_id, signal.chat_title)
    ):
        return signal, 0.0
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
    if not entries:
        entries = [float(signal.entry or market_price)]
    anchor = max(entries) if signal.side == "buy" else min(entries)
    shift = float(market_price) - anchor
    if abs(shift) <= 2.0:
        return signal, 0.0

    live_tps = [
        float(value)
        for value in signal.tps
        if (signal.side == "buy" and float(value) > anchor)
        or (signal.side == "sell" and float(value) < anchor)
    ]
    if not live_tps or any(abs(value - anchor) > 50.0 for value in live_tps):
        return signal, 0.0
    raw_sl = float(signal.sl or 0.0)
    if raw_sl > 0.0:
        valid_sl = raw_sl < anchor if signal.side == "buy" else raw_sl > anchor
        if not valid_sl or abs(raw_sl - anchor) > 40.0:
            return signal, 0.0

    shifted_tps = [float(value) + shift for value in signal.tps]
    return (
        replace(
            signal,
            entry=float(signal.entry or anchor) + shift,
            entries=[float(value) + shift for value in entries],
            sl=raw_sl + shift if raw_sl > 0.0 else 0.0,
            tp=float(signal.tp or signal.tps[0]) + shift,
            tps=shifted_tps,
        ),
        shift,
    )


def _ghp_gold_sanity_reason(signal: ParsedSignal, market_price: float) -> str | None:
    """Reject GHP XAU levels that remain implausible after quote normalization."""
    if signal.asset != "gold" or not is_ghp_source(signal.chat_id, signal.chat_title):
        return None
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
    if not entries:
        entries = [float(signal.entry or 0.0)]
    entries = [value for value in entries if value > 0.0]
    if not entries:
        return "ghp_gold_missing_entry"
    sl = float(signal.sl or 0.0)
    if sl <= 0.0:
        return "ghp_gold_missing_sl"
    min_stop_distance = max(0.0, _env_float("GHP_GOLD_MIN_STOP_DISTANCE_USD", 2.0))
    if min(abs(entry - sl) for entry in entries) < min_stop_distance:
        return "ghp_gold_suspicious_tight_stop_wait_for_edit"
    max_stop_distance = max(1.0, _env_float("GHP_GOLD_MAX_STOP_DISTANCE_USD", 40.0))
    if min(abs(entry - sl) for entry in entries) > max_stop_distance:
        return "ghp_gold_implausible_stop_distance"
    if signal.order_kind == "market" and market_price > 0.0:
        max_market_distance = max(1.0, _env_float("GHP_GOLD_MAX_ENTRY_MARKET_DISTANCE_USD", 35.0))
        if min(abs(entry - float(market_price)) for entry in entries) > max_market_distance:
            return "ghp_gold_implausible_market_distance"
    return None


def _strict_live_tps_for_entry(side: str, entry: float, tps: list[float]) -> list[float]:
    entry_value = float(entry or 0.0)
    if entry_value <= 0:
        return []
    if side == "buy":
        return sorted(float(value) for value in tps if float(value) > entry_value)
    return sorted((float(value) for value in tps if float(value) < entry_value), reverse=True)


def _repair_tps_for_entry(side: str, entry: float, tps: list[float], min_targets: int = 4) -> list[float]:
    live_tps = _strict_live_tps_for_entry(side, entry, tps)
    if len(live_tps) >= min_targets:
        return live_tps

    distances = [abs(float(value) - float(entry)) for value in live_tps if abs(float(value) - float(entry)) >= 0.2]
    step = min(distances) if distances else 1.0
    step = max(1.0, min(float(step), 5.0))
    next_tp = float(live_tps[-1]) if live_tps else float(entry)
    while len(live_tps) < max(1, int(min_targets)):
        next_tp = next_tp + step if side == "buy" else next_tp - step
        live_tps.append(round(next_tp, 2))
    return _strict_live_tps_for_entry(side, entry, live_tps)


def _level_ladder_valid_for_side(side: str, entries: list[float], sl: float, tps: list[float]) -> bool:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0]
    clean_tps = [float(value) for value in tps if float(value or 0.0) > 0]
    if not clean_entries or not clean_tps or sl <= 0:
        return False
    low = min(clean_entries)
    high = max(clean_entries)
    if side == "buy":
        return float(sl) < low and all(float(tp) > high for tp in clean_tps)
    return float(sl) > high and all(float(tp) < low for tp in clean_tps)


def _phoenix_tp_ladder_is_strict(side: str, tps: list[float]) -> bool:
    clean_tps = [float(value) for value in tps if float(value or 0.0) > 0]
    if len(clean_tps) < 2:
        return True
    if side == "buy":
        return all(next_tp > current_tp + 0.01 for current_tp, next_tp in zip(clean_tps, clean_tps[1:]))
    if side == "sell":
        return all(next_tp < current_tp - 0.01 for current_tp, next_tp in zip(clean_tps, clean_tps[1:]))
    return False


def _repair_gold_hundred_digit_typo(signal: ParsedSignal, market_price: float) -> ParsedSignal:
    if signal.asset != "gold" or market_price <= 0 or not signal.entries or not signal.tps:
        return signal
    if _is_explicit_provider_pending(signal):
        return signal
    clean_entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    if not clean_entries:
        return signal
    original_distance = min(abs(float(entry) - float(market_price)) for entry in clean_entries)
    if original_distance < 20.0:
        return signal
    best: tuple[float, ParsedSignal] | None = None

    # Some channels occasionally typo one digit in all levels (e.g. 4086 instead of 4186).
    # Test uniform shifts against live market while preserving the signal's risk/reward ladder.
    deltas = [float(value) for value in range(-300, 301, 10) if value != 0]
    deltas.extend(float(value) for value in range(-9, 10) if value != 0)
    deltas = sorted(set(deltas), key=lambda value: (abs(value), value))
    for delta in deltas:
        shifted_entries = [round(float(value) + delta, 2) for value in signal.entries]
        shifted_tps = [round(float(value) + delta, 2) for value in signal.tps]
        shifted_sl = round(float(signal.sl or 0.0) + delta, 2) if signal.sl > 0 else 0.0
        if not _level_ladder_valid_for_side(signal.side, shifted_entries, shifted_sl, shifted_tps):
            continue
        shifted_distance = min(abs(float(entry) - float(market_price)) for entry in shifted_entries)
        if signal.side == "buy" and max(shifted_entries) > float(market_price) + 2.0:
            continue
        if signal.side == "sell" and min(shifted_entries) < float(market_price) - 2.0:
            continue
        if shifted_distance > 20.0 or shifted_distance >= original_distance - 8.0:
            continue
        repaired = replace(
            signal,
            entry=round(float(signal.entry or clean_entries[0]) + delta, 2),
            entries=shifted_entries,
            sl=shifted_sl,
            tp=shifted_tps[0],
            tps=shifted_tps,
        )
        if best is None or shifted_distance < best[0]:
            best = (shifted_distance, repaired)
    return best[1] if best is not None else signal


def _repair_phoenix_truncated_stop(signal: ParsedSignal, market_price: float) -> ParsedSignal:
    """Recover a visibly truncated four-digit Phoenix SL, e.g. ``438`` -> ``4380``."""
    if signal.asset != "gold" or signal.sl > 0.0 or market_price <= 0.0 or not signal.entries:
        return signal
    match = re.search(
        r"(?im)^\s*(?:sl|s/l|s\.l\.?|stop\s*loss)\s*[:=\-]?\s*(\d{3})\s*$",
        str(signal.raw_text or ""),
    )
    if not match:
        return signal
    prefix = int(match.group(1))
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
    if not entries:
        return signal
    edge = min(entries) if signal.side == "buy" else max(entries)
    expected_gap = max(1.0, _env_float("PHOENIX_PROVIDER_SL_EXPECTED_BUFFER_USD", 3.0))
    candidates = []
    for last_digit in range(10):
        candidate = float(prefix * 10 + last_digit)
        if not _valid_stop_for_side(signal.side, edge, candidate):
            continue
        gap = abs(candidate - edge)
        if gap < 1.0 or gap > 12.0 or abs(candidate - market_price) > 25.0:
            continue
        candidates.append((abs(gap - expected_gap), gap, candidate))
    if not candidates:
        return signal
    _, _, repaired_sl = min(candidates)
    return replace(signal, sl=round(repaired_sl, 2))


def _is_explicit_provider_pending(signal: ParsedSignal) -> bool:
    if str(signal.order_kind or "").lower() not in {"limit", "stop"}:
        return False
    normalized = _normalize_text(signal.raw_text or "")
    return bool(re.search(r"\b(?:BUY|SELL)\s+(?:LIMIT|STOP)\b", normalized))


def _phoenix_levels_plausible_against_market(signal: ParsedSignal, market_price: float) -> bool:
    if market_price <= 0:
        return False
    levels: list[float] = []
    levels.extend(float(value) for value in signal.entries if float(value or 0.0) > 0)
    levels.extend(float(value) for value in signal.tps if float(value or 0.0) > 0)
    if float(signal.sl or 0.0) > 0:
        levels.append(float(signal.sl))
    if not levels:
        return False
    max_distance = _env_float("PHOENIX_MAX_LEVEL_MARKET_DISTANCE", PHOENIX_MAX_LEVEL_MARKET_DISTANCE)
    return min(abs(float(level) - float(market_price)) for level in levels) <= max_distance


def _market_near_entry_zone(entries: list[float], market_price: float, tolerance: float = NEAR_ENTRY_MARKET_TOLERANCE) -> bool:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0]
    if not clean_entries or market_price <= 0:
        return False
    low = min(clean_entries)
    high = max(clean_entries)
    return (low - tolerance) <= float(market_price) <= (high + tolerance)


def _phoenix_continuation_market_allowed(side: str, entries: list[float], market_price: float, tps: list[float]) -> bool:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0]
    live_tps = _strict_live_tps_for_entry(side, market_price, tps)
    if not clean_entries or len(live_tps) < 3 or market_price <= 0:
        return False
    low = min(clean_entries)
    high = max(clean_entries)
    if side == "buy":
        return market_price > high and market_price < float(live_tps[2])
    return market_price < low and market_price > float(live_tps[2])


def _phoenix_market_runner_allowed(side: str, entries: list[float], market_price: float, tps: list[float]) -> bool:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0]
    live_tps = _strict_live_tps_for_entry(side, market_price, tps)
    if not clean_entries or not live_tps or market_price <= 0:
        return False
    low = min(clean_entries)
    high = max(clean_entries)
    tolerance = _env_float("PHOENIX_MARKET_ENTRY_TOLERANCE", PHOENIX_MARKET_ENTRY_TOLERANCE)
    if not (low - tolerance <= float(market_price) <= high + tolerance):
        return False
    tp1_distance = abs(float(live_tps[0]) - float(market_price))
    min_distance = _env_float("PHOENIX_MIN_MARKET_TP1_DISTANCE", PHOENIX_MIN_MARKET_TP1_DISTANCE)
    return tp1_distance >= min_distance


def _phoenix_extra_market_runner_target(
    side: str,
    entries: list[float],
    market_price: float,
    tps: list[float],
    target_index: int,
    gate: str = "strict_zone",
) -> int:
    target = max(1, int(target_index))
    live_tps = _strict_live_tps_for_entry(side, market_price, tps)
    normalized_gate = str(gate or "strict_zone").strip().lower()
    if normalized_gate == "before_tp1":
        allowed = bool(live_tps)
    else:
        allowed = _phoenix_market_runner_allowed(side, entries, market_price, tps)
    if not allowed:
        return 0
    return target if len(live_tps) >= target else 0


def _phoenix_deepest_runner_target(side: str, market_price: float, tps: list[float]) -> int:
    live_count = len(_strict_live_tps_for_entry(side, market_price, tps))
    min_target = max(1, int(_env_float("PHOENIX_DEEP_RUNNER_MIN_TARGET", 5.0)))
    max_target = max(min_target, int(_env_float("PHOENIX_DEEP_RUNNER_MAX_TARGET", 8.0)))
    if live_count < min_target:
        return 0
    return min(max_target, live_count)


def _strategy_market_entry_allowed(
    strategy: ChannelStrategy,
    side: str,
    entries: list[float],
    market_price: float,
    tps: list[float],
) -> bool:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0]
    if market_price <= 0 or not clean_entries:
        return False
    live_tps = _strict_live_tps_for_entry(side, market_price, tps)
    if not live_tps:
        return False
    tolerance = float(strategy.strict_market_tolerance or NEAR_ENTRY_MARKET_TOLERANCE)
    if not _market_near_entry_zone(clean_entries, market_price, tolerance):
        return False
    min_tp1_distance = float(strategy.min_market_tp1_distance or 0.0)
    if min_tp1_distance > 0.0 and abs(float(live_tps[0]) - float(market_price)) < min_tp1_distance:
        return False
    return True


def _market_before_live_tp1(side: str, market_price: float, tps: list[float]) -> bool:
    if market_price <= 0:
        return False
    live_tps = _strict_live_tps_for_entry(side, market_price, tps)
    if not live_tps:
        return False
    tp1 = float(live_tps[0])
    return market_price < tp1 if side == "buy" else market_price > tp1


def _tp_one_runner_target_index(
    side: str,
    market_price: float,
    tps: list[float],
    spread_price: float,
) -> int:
    """Pick the first live TP that still pays for entering at market."""
    live_tps = _strict_live_tps_for_entry(side, market_price, tps)
    if not live_tps:
        return 0
    min_reward = max(
        _env_float("SIGNAL_TP_ONE_RUNNER_MIN_REWARD_USD", 0.5),
        max(0.0, float(spread_price)) * 2.0,
    )
    for index, tp in enumerate(live_tps, start=1):
        if abs(float(tp) - float(market_price)) + 1e-9 >= min_reward:
            return index
    return len(live_tps)


def _tp_one_runner_requested_volume(
    ordinary_leg_volume: float,
    fixed_volume: float,
    multiplier: float,
) -> float:
    if multiplier > 0.0:
        return max(0.0, float(ordinary_leg_volume)) * float(multiplier)
    return max(0.0, float(fixed_volume))


def _strategy_uses_market_before_tp1(strategy: ChannelStrategy) -> bool:
    return "market_before_tp1" in str(strategy.name or "").lower()


def _market_order_allowed_for_strategy(
    strategy: ChannelStrategy,
    side: str,
    planned_entry: float,
    market_price: float,
    tps: list[float],
) -> bool:
    if float(strategy.strict_market_tolerance or 0.0) <= 0.0 and float(strategy.min_market_tp1_distance or 0.0) <= 0.0:
        return True
    return _strategy_market_entry_allowed(strategy, side, [float(planned_entry)], market_price, tps)


def _review_execution_order_block_reason(review_execution_mode: str, order_kind: str) -> str | None:
    if str(review_execution_mode or "").lower() == "provider_pending" and str(order_kind or "").lower() == "market":
        return "review_provider_pending_market_block"
    return None


def _looks_like_complete_provider_signal_text(text: str) -> bool:
    value = str(text or "")
    return bool(
        SIDE_RE.search(value)
        and re.search(r"\b(?:entry|zone|focus)\b\s*[:=@-]?\s*\d", value, re.I)
        and re.search(r"\b(?:sl|s/l|stop\s*loss)\b\s*[:=@-]?\s*\d", value, re.I)
        and re.search(r"\b(?:tp\s*\d*|targets?|take\s*profit)\b\s*[:=@-]?\s*\d", value, re.I)
    )


def _market_tp1_reward_ok(side: str, market_price: float, sl: float, tp1: float, min_rr: float = MIN_MARKET_TP1_RR) -> bool:
    if market_price <= 0 or sl <= 0 or tp1 <= 0:
        return True
    reward = abs(float(tp1) - float(market_price))
    risk = abs(float(market_price) - float(sl))
    if risk <= 0:
        return False
    if side == "buy" and (float(tp1) <= float(market_price) or float(sl) >= float(market_price)):
        return False
    if side == "sell" and (float(tp1) >= float(market_price) or float(sl) <= float(market_price)):
        return False
    return (reward / risk) >= float(min_rr)


def _market_tp1_spread_reward_ok(symbol_name: str, entry_price: float, tp1: float, spread_points: float) -> tuple[bool, float, float, float]:
    info = mt5.symbol_info(symbol_name)
    point = float(getattr(info, "point", 0.01) or 0.01) if info is not None else 0.01
    spread_price = max(0.0, float(spread_points) * point)
    reward = abs(float(tp1) - float(entry_price))
    net_reward = reward - spread_price
    min_net = _env_float("SIGNAL_MIN_NET_TP1_AFTER_SPREAD_USD", 0.0)
    min_mult = _env_float("SIGNAL_MIN_TP1_SPREAD_MULT", 0.0)
    required_reward = max(float(min_net) + spread_price, spread_price * float(min_mult))
    if required_reward <= 0.0:
        return True, reward, net_reward, required_reward
    return reward + 1e-9 >= required_reward, reward, net_reward, required_reward


def _with_nearest_runner_leg(
    placement_plan: list[tuple[int, float, int, str, int]],
    market_price: float,
    target_index: int = 4,
    protect_mode: str = "atr",
) -> list[tuple[int, float, int, str, int]]:
    if not placement_plan:
        return placement_plan
    runner_pos = 0
    for pos, (entry_index, _entry, _target, _protect, _plan_index) in enumerate(placement_plan):
        if entry_index == 0:
            runner_pos = pos
            break
    else:
        runner_pos = min(range(len(placement_plan)), key=lambda pos: abs(float(placement_plan[pos][1]) - float(market_price)))
    entry_index, entry, _old_target, _old_protect, plan_index = placement_plan[runner_pos]
    updated = list(placement_plan)
    updated[runner_pos] = (entry_index, entry, int(target_index), str(protect_mode), plan_index)
    return updated


def _phoenix_progressive_stop(side: str, entry: float, tps: list[float], hit_level: int, current_sl: float) -> float | None:
    if entry <= 0 or hit_level <= 0:
        return None
    if hit_level == 1:
        candidate = float(entry) + PHOENIX_BE_BUFFER_USD if side == "buy" else float(entry) - PHOENIX_BE_BUFFER_USD
    else:
        clean_tps = [float(value) for value in tps if float(value or 0.0) > 0]
        if not clean_tps:
            return None
        candidate = clean_tps[min(max(0, int(hit_level) - 2), len(clean_tps) - 1)]
    return _better_stop(side, current_sl, round(float(candidate), 2))


def _phoenix_reached_tp_level(side: str, current_price: float, tps: list[float]) -> int:
    reached = 0
    for index, tp in enumerate([float(value) for value in tps if float(value or 0.0) > 0], start=1):
        if _price_reached_tp(side, float(current_price), float(tp)):
            reached = index
    return reached


def _phoenix_entry_brain(signal: ParsedSignal, market_price: float, entries: list[float]) -> dict:
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0]
    live_tps = _strict_live_tps_for_entry(signal.side, market_price, signal.tps)
    reached_level = _phoenix_reached_tp_level(signal.side, market_price, signal.tps)
    tolerance = _env_float("PHOENIX_MARKET_ENTRY_TOLERANCE", PHOENIX_MARKET_ENTRY_TOLERANCE)
    max_late_level = int(_env_float("PHOENIX_MAX_MARKET_REACHED_TP_LEVEL", 2.0))
    min_live_tps = int(_env_float("PHOENIX_MIN_LIVE_TPS_TO_CHASE", 2.0))
    if not clean_entries:
        return {
            "decision": "no_entry_levels",
            "live_tps": live_tps,
            "reached_level": reached_level,
            "zone_state": "unknown",
        }
    low = min(clean_entries)
    high = max(clean_entries)
    zone_distance = 0.0
    if market_price < low:
        zone_state = "before_zone" if signal.side == "buy" else "after_zone"
        zone_distance = low - market_price
    elif market_price > high:
        zone_state = "after_zone" if signal.side == "buy" else "before_zone"
        zone_distance = market_price - high
    else:
        zone_state = "inside_zone"
    if reached_level >= max_late_level + 1:
        decision = "skip_too_late_after_tp"
    elif reached_level > 0 and len(live_tps) < min_live_tps:
        decision = "skip_not_enough_live_tps"
    elif zone_state == "inside_zone" or zone_distance <= tolerance:
        decision = "fresh_zone"
    elif reached_level > 0:
        decision = "continuation_after_tp"
    else:
        decision = "pending_only"
    return {
        "decision": decision,
        "live_tps": live_tps,
        "reached_level": reached_level,
        "zone_state": zone_state,
        "zone_distance": round(float(zone_distance), 3),
        "zone_low": low,
        "zone_high": high,
        "tolerance": tolerance,
        "max_late_level": max_late_level,
        "min_live_tps": min_live_tps,
    }


def _phoenix_wait_for_zone_retrace(brain: dict) -> bool:
    """Use pending-only execution when price is materially beyond the entry zone."""
    always_stage = _env_bool("PHOENIX_ALWAYS_STAGE_RANGE_PENDING", False)
    # A quote that is just outside the range but still within the configured
    # tolerance is a fresh signal. It should keep the staged range entries and
    # may also open the market runner; treating it as retrace-only caused valid
    # Phoenix signals to be represented solely by expiring pending orders.
    if str(brain.get("decision") or "") == "fresh_zone":
        return False
    return bool(
        str(brain.get("zone_state") or "") == "after_zone"
        and (always_stage or int(brain.get("reached_level", 0) or 0) == 0)
        and float(brain.get("zone_distance", 0.0) or 0.0) > 0.0
    )


def _phoenix_full_signal_stage_allowed(brain: dict, retrace_pending: bool, entry_count: int) -> bool:
    """Allow a fresh TP1 retrace, but never revive a Phoenix signal at TP2 or later."""
    if not retrace_pending or int(entry_count) <= 1:
        return False
    if not _env_bool("PHOENIX_ALWAYS_STAGE_RANGE_PENDING", False):
        return False
    max_reached_level = max(
        0,
        int(_env_float("PHOENIX_FULL_SIGNAL_STAGE_MAX_REACHED_TP_LEVEL", 1.0)),
    )
    return int(brain.get("reached_level", 0) or 0) <= max_reached_level


def _phoenix_pending_stage_allowed(
    signal: ParsedSignal,
    brain: dict,
    retrace_pending: bool,
    entry_count: int,
    preliminary_range_matched: bool = False,
) -> bool:
    if _is_explicit_provider_pending(signal):
        return bool(retrace_pending and int(entry_count) > 0)
    if preliminary_range_matched and _env_bool("PHOENIX_PLAY_FULL_SIGNAL_AFTER_PRE_RANGE", True):
        return bool(retrace_pending and int(entry_count) > 1)
    return _phoenix_full_signal_stage_allowed(brain, retrace_pending, entry_count)


def _phoenix_followup_revision_match(
    managed: dict,
    signal: ParsedSignal,
    now: datetime | None = None,
    window_seconds: float = 120.0,
    tp1_tolerance: float = 0.10,
    entry_tolerance: float = 2.0,
) -> bool:
    """Identify a rapid corrected Phoenix post without merging separate signals."""
    if not _is_phoenix_source(signal.chat_id, signal.chat_title):
        return False
    if int(managed.get("chat_id", 0) or 0) != int(signal.chat_id or 0):
        return False
    if str(managed.get("side", "") or "").lower() != str(signal.side or "").lower():
        return False
    source_message_id = _managed_source_message_id(managed)
    if source_message_id <= 0 or source_message_id >= int(signal.message_id or 0):
        return False
    try:
        created = datetime.fromisoformat(str(managed.get("created_utc", "")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    age_seconds = ((now or datetime.now(UTC)) - created).total_seconds()
    if age_seconds < 0 or age_seconds > max(1.0, float(window_seconds)):
        return False
    signal_tps = [float(value) for value in signal.tps if float(value or 0.0) > 0.0]
    managed_tp1 = float(managed.get("tp1", 0.0) or 0.0)
    if not signal_tps or managed_tp1 <= 0 or abs(managed_tp1 - signal_tps[0]) > max(0.0, float(tp1_tolerance)):
        return False
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
    managed_entry = float(managed.get("entry", 0.0) or 0.0)
    if not entries or managed_entry <= 0:
        return False
    low, high = min(entries), max(entries)
    tolerance = max(0.0, float(entry_tolerance))
    return low - tolerance <= managed_entry <= high + tolerance


def _phoenix_preliminary_reconcile_reason(
    position_side: str,
    entry: float,
    profit: float,
    signal_side: str,
    signal_entries: list[float],
    adverse_gap_usd: float,
) -> str | None:
    if position_side not in {"buy", "sell"} or signal_side not in {"buy", "sell"}:
        return None
    if position_side != signal_side:
        return "opposite_full_signal"
    if profit >= 0:
        return None
    clean_entries = [float(value) for value in signal_entries if float(value or 0.0) > 0]
    if not clean_entries:
        return None
    low, high = min(clean_entries), max(clean_entries)
    gap = float(entry) - high if signal_side == "buy" else low - float(entry)
    if gap >= max(0.0, float(adverse_gap_usd)):
        return "entry_worse_than_full_zone"
    return None


def _phoenix_runner_timeout_due(age_seconds: float, profit: float, max_hold_minutes: float) -> bool:
    return (
        max_hold_minutes > 0
        and age_seconds >= max_hold_minutes * 60.0
        and profit < 0
    )


def _phoenix_runner_matches_range(
    side: str,
    runner_entry: float,
    entry_range: list[float],
    tolerance_usd: float,
) -> bool:
    clean_range = [float(value) for value in entry_range if float(value or 0.0) > 0]
    if side not in {"buy", "sell"} or len(clean_range) != 2:
        return False
    low, high = min(clean_range), max(clean_range)
    tolerance = max(0.0, float(tolerance_usd))
    return runner_entry <= high + tolerance if side == "buy" else runner_entry >= low - tolerance


def _tfxc_premium_tight_signal(signal: ParsedSignal) -> ParsedSignal:
    entry = float(signal.entry or 0.0)
    if entry <= 0 or not signal.tps:
        return signal
    live_tps = _normalize_tps_for_side(signal.side, entry, signal.tps)
    if not live_tps:
        return signal
    tp_distance = abs(float(live_tps[0]) - entry)
    if tp_distance <= 0:
        return signal

    risk_distance = max(1.5, tp_distance * 1.2)
    if signal.sl > 0:
        risk_distance = min(risk_distance, abs(float(signal.sl) - entry))

    if signal.side == "buy":
        sl = entry - risk_distance
        tps = [entry + tp_distance, entry + (tp_distance * 1.5)]
    else:
        sl = entry + risk_distance
        tps = [entry - tp_distance, entry - (tp_distance * 1.5)]
    tps = _normalize_tps_for_side(signal.side, entry, tps)
    if not tps:
        return signal
    return replace(signal, sl=round(sl, 2), tp=float(tps[0]), tps=[round(float(tp), 2) for tp in tps])


def _tfxc_momentum_signal(signal: ParsedSignal, market_price: float) -> ParsedSignal | None:
    if market_price <= 0 or not signal.tps:
        return None
    first_tp = float(signal.tps[0])
    if not _price_reached_tp(signal.side, market_price, first_tp):
        return None
    live_tps = (
        [float(tp) for tp in signal.tps if float(tp) > market_price]
        if signal.side == "buy"
        else [float(tp) for tp in signal.tps if float(tp) < market_price]
    )
    if not live_tps:
        return None
    next_tp = float(live_tps[0])
    if abs(next_tp - float(market_price)) < TFXC_MIN_MOMENTUM_TP_DISTANCE:
        return None
    return replace(
        signal,
        entry=round(float(market_price), 2),
        entries=[round(float(market_price), 2)],
        sl=round(first_tp, 2),
        tp=next_tp,
        tps=live_tps,
        order_kind="market",
        order_type=_order_type(signal.side, "market"),
    )


def _sl_increases_risk(side: str, old_sl: float, new_sl: float) -> bool:
    if old_sl <= 0.0 or new_sl <= 0.0:
        return False
    if side == "buy":
        return float(new_sl) < float(old_sl) - 0.01
    return float(new_sl) > float(old_sl) + 0.01


def _split_target_plan_for_strategy(strategy: ChannelStrategy, *, is_phoenix_signal: bool, is_tfxc_signal: bool) -> list[tuple[int, str, int]]:
    if not strategy.split_target_indices:
        return [
            (target_index, protect_mode, plan_index)
            for plan_index, (target_index, protect_mode) in enumerate(THREE_LEG_TARGET_PLAN, start=1)
        ]
    target_plan: list[tuple[int, str, int]] = []
    phoenix_protect_override = tuple(
        value.strip().lower()
        for value in str(os.getenv("PHOENIX_SPLIT_PROTECT_MODES", "") or "").split(",")
        if value.strip()
    )
    for plan_index, target_index in enumerate(strategy.split_target_indices, start=1):
        if is_phoenix_signal and plan_index <= len(phoenix_protect_override):
            protect_mode = phoenix_protect_override[plan_index - 1]
        elif plan_index <= len(strategy.split_protect_modes):
            protect_mode = str(strategy.split_protect_modes[plan_index - 1] or "none")
        elif is_tfxc_signal:
            protect_mode = str(strategy.protect_mode or "none")
        elif is_phoenix_signal and int(target_index) >= 6:
            protect_mode = "phoenix_ladder"
        elif is_phoenix_signal and int(target_index) >= 3:
            protect_mode = "be"
        else:
            protect_mode = "be" if plan_index > 1 else "none"
        target_plan.append((int(target_index), protect_mode, plan_index))
    return target_plan


def _phoenix_entries_for_target_plan(
    side: str,
    entries: list[float],
    target_count: int,
    mode: str,
) -> list[float]:
    levels = sorted({round(float(value), 3) for value in entries if float(value or 0.0) > 0.0})
    if not levels or target_count <= 0:
        return []
    if len(levels) == 2:
        levels.insert(1, round((levels[0] + levels[1]) / 2.0, 3))
    if len(levels) > 3:
        levels = [levels[0], levels[len(levels) // 2], levels[-1]]
    normalized_mode = str(mode or "legacy").strip().lower()
    near_to_far = list(reversed(levels)) if side == "buy" else list(levels)
    if normalized_mode == "near":
        return [near_to_far[0]] * int(target_count)
    if normalized_mode == "side_deep":
        return (near_to_far + [near_to_far[-1]] * int(target_count))[: int(target_count)]
    if normalized_mode == "cycle":
        return [near_to_far[index % len(near_to_far)] for index in range(int(target_count))]
    if normalized_mode == "middle":
        return [levels[len(levels) // 2]] * int(target_count)
    return (levels + [levels[-1]] * int(target_count))[: int(target_count)]


def _phoenix_profit_module_plan(
    entries: list[float],
    targets: tuple[int, ...] = (1, 5, 6),
    protect_modes: tuple[str, ...] = (),
) -> list[tuple[int, float, int, str, int]]:
    """Build the independent lower-WR Phoenix package at the zone midpoint."""
    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0.0]
    clean_targets = [max(1, int(value)) for value in targets if int(value) > 0]
    if not clean_entries or not clean_targets:
        return []
    middle = round((min(clean_entries) + max(clean_entries)) / 2.0, 3)
    return [
        (
            910 + offset,
            middle,
            target,
            protect_modes[offset - 1] if offset <= len(protect_modes) else "none",
            910 + offset,
        )
        for offset, target in enumerate(clean_targets, start=1)
    ]


def _extract_first_float(regex: re.Pattern[str], text: str) -> float | None:
    match = regex.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _is_noise(text: str) -> bool:
    return bool(NOISE_RE.search(text)) and not _looks_like_signal_candidate(text)


def _looks_like_signal_candidate(text: str, chat_title: str = "") -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if looks_like_ghp_signal(normalized, chat_title):
        return True
    has_asset = bool(GOLD_ASSET_RE.search(normalized) or NAS100_ASSET_RE.search(normalized) or US30_ASSET_RE.search(normalized) or BTC_ASSET_RE.search(normalized))
    has_direction_or_entry = bool(SIDE_RE.search(normalized) or ENTRY_RE.search(normalized) or ENTRY_RANGE_RE.search(normalized))
    has_target = bool(TP_LABEL_RE.search(normalized))
    return has_asset and has_direction_or_entry and has_target and len(PRICE_VALUE_RE.findall(normalized)) >= 2


def _mentions_gold_asset(text: str) -> bool:
    return bool(GOLD_ASSET_RE.search(text))


def _detect_asset(text: str, chat_title: str = "") -> str | None:
    if GOLD_ASSET_RE.search(text):
        return "gold"
    if BTC_ASSET_RE.search(text):
        return "btc"
    if US30_ASSET_RE.search(text):
        return "us30"
    if NAS100_ASSET_RE.search(text):
        return "nas100"
    if GOLD_ASSET_RE.search(chat_title):
        return "gold"
    if BTC_ASSET_RE.search(chat_title):
        return "btc"
    if US30_ASSET_RE.search(chat_title):
        return "us30"
    if NAS100_ASSET_RE.search(chat_title):
        return "nas100"
    return None


def _extract_side_line_entry(text: str) -> float | None:
    for line in text.splitlines():
        if not SIDE_RE.search(line):
            continue
        if re.search(r"\b(?:tp|take\s*profits?|target|targets|tgt|sl|s/l|stop\s*loss)\b", line, re.I):
            continue
        prices = _line_prices(line)
        if prices:
            return prices[0]
    return None


def _order_type(side: str, order_kind: str) -> int:
    if order_kind == "limit":
        return mt5.ORDER_TYPE_BUY_LIMIT if side == "buy" else mt5.ORDER_TYPE_SELL_LIMIT
    if order_kind == "stop":
        return mt5.ORDER_TYPE_BUY_STOP if side == "buy" else mt5.ORDER_TYPE_SELL_STOP
    return mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL


def _parse_signal(
    event_text: str,
    uid: str,
    chat_id: int | None,
    chat_title: str,
    post_author: str,
    message_id: int,
    side_hint: str | None = None,
) -> ParsedSignal | None:
    text = _normalize_text(event_text)
    if not text:
        return None
    if _is_noise(text):
        return None
    dany_signals_source = _is_dany_signals_source(chat_id, chat_title)
    if is_ghp_source(chat_id, chat_title) or dany_signals_source:
        # Dany Signals is our curated relay. Messages without an explicit
        # symbol use the gold convention; explicit FX/index symbols still win.
        ghp_title = "Goldhunter Paul curated signals" if dany_signals_source else chat_title
        ghp_text = _normalize_dany_signal_targets(event_text) if dany_signals_source else event_text
        ghp_message = parse_ghp_message(ghp_text, ghp_title)
        ghp_signal = ghp_message.signal
        if ghp_message.kind == "signal" and ghp_signal is not None:
            entries = [float(value) for value in ghp_signal.entries]
            tps = [float(value) for value in ghp_signal.tps]
            signal_hash = hashlib.sha1(
                f"{uid}|{ghp_signal.side}|{ghp_signal.order_kind}|{entries}|{ghp_signal.sl}|{tps}|{text}".encode("utf-8")
            ).hexdigest()[:12]
            return ParsedSignal(
                uid=f"{uid}:{signal_hash}",
                chat_id=chat_id,
                chat_title=chat_title,
                post_author=post_author,
                message_id=message_id,
                side=ghp_signal.side,
                asset=ghp_signal.asset,
                entry=float(ghp_signal.entry),
                entries=entries,
                sl=float(ghp_signal.sl),
                tp=float(tps[0]),
                tps=tps,
                order_type=_order_type(ghp_signal.side, ghp_signal.order_kind),
                order_kind=ghp_signal.order_kind,
                raw_text=event_text,
            )
    asset = _detect_asset(text, chat_title)
    if asset is None:
        return None

    order_kind = _extract_order_kind(text)
    tagsignals_source = _is_tagsignals_source(chat_id, chat_title)
    xauusd_gold_signal_source = _is_xauusd_gold_signal_source(chat_id, chat_title)
    mthri_source = _is_mthri_source(chat_id, chat_title)
    gold_signal_provide_source = _is_gold_signal_provide_source(chat_id, chat_title)
    entry_range = _extract_entry_range(
        event_text,
        allow_bare=tagsignals_source,
        allow_side_bare=True,
    )
    entry = float(entry_range[0]) if entry_range else _extract_float(ENTRY_RE, event_text)
    if entry is None:
        entry = _extract_float(INLINE_PENDING_ENTRY_RE, text)
    if entry is None:
        entry = _extract_side_line_entry(event_text)
    if order_kind in {"limit", "stop"} and entry is None:
        return None

    sl = _extract_float(SL_RE, event_text)
    tps = _extract_tp_values(event_text)
    side = _extract_side(text)
    if _is_phoenix_source(chat_id, chat_title) and entry_range and tps:
        repair_side = side or (str(side_hint or "").strip().lower() if side_hint else None)
        if repair_side not in {"buy", "sell"}:
            repair_side = _infer_side_from_levels(entry, None, tps, entry_range)
        if repair_side in {"buy", "sell"}:
            sl = _repair_phoenix_sl_digit_typo(repair_side, _build_entry_prices(entry, entry_range), sl)
    inferred_side = _infer_side_from_levels(entry, sl, tps, entry_range)
    if side and inferred_side and side != inferred_side and entry and sl and len(tps) >= 2:
        # Some providers occasionally mistype BUY/SELL while the complete
        # TP ladder and SL still describe an unambiguous opposite trade.
        side = inferred_side
    normalized_hint = str(side_hint or "").strip().lower()
    if normalized_hint not in {"buy", "sell"}:
        normalized_hint = ""
    if side is None and normalized_hint:
        if inferred_side and inferred_side != normalized_hint:
            return None
        side = normalized_hint
    if side is None:
        side = inferred_side
    if side is None:
        return None
    if xauusd_gold_signal_source and entry_range:
        zone_tps, zone_sl = _levels_from_side_zone(side, entry_range, event_text)
        if zone_tps:
            tps = zone_tps
        if (sl is None or sl <= 0) and zone_sl > 0:
            sl = zone_sl
    if not tps and tagsignals_source and entry_range:
        tps = _synthetic_zone_tps(side, entry_range)
    if not tps:
        return None
    if (sl is None or sl <= 0) and tagsignals_source and entry_range:
        sl = _synthetic_zone_sl(side, entry_range)
    tps = _normalize_tps_for_side(side, entry, tps)
    tp = float(tps[0])
    entries = _build_entry_prices(entry, entry_range)
    if not _gold_levels_sane(asset, entry, entries, sl, tps):
        return None

    signal_hash = hashlib.sha1(f"{uid}|{side}|{order_kind}|{entries}|{sl}|{tp}|{text}".encode("utf-8")).hexdigest()[:12]
    return ParsedSignal(
        uid=f"{uid}:{signal_hash}",
        chat_id=chat_id,
        chat_title=chat_title,
        post_author=post_author,
        message_id=message_id,
        side=side,
        asset=asset,
        entry=float(entry or 0.0),
        entries=entries,
        sl=float(sl or 0.0),
        tp=float(tp),
        tps=[float(value) for value in tps],
        order_type=_order_type(side, order_kind),
        order_kind=order_kind,
        raw_text=event_text,
    )


def _auto_stop_loss(
    symbol_name: str,
    side: str,
    entry_price: float,
    spread_points: float,
    atr_mult: float,
    min_points: int,
) -> float:
    try:
        frame = get_rates_df(symbol_name, "M5", 180)
        enriched = enrich(frame)
        atr_value = float(enriched.iloc[-1]["atr14"] or 0.0)
    except Exception:
        atr_value = 0.0

    info = mt5.symbol_info(symbol_name)
    point = float(getattr(info, "point", 0.01) or 0.01)
    spread_buffer = max(point * float(min_points), spread_points * point * 3.0)
    atr_buffer = atr_value * max(1.0, atr_mult) if atr_value > 0 else 0.0
    offset = max(spread_buffer, atr_buffer, point * float(min_points))
    if side == "buy":
        return round(entry_price - offset, getattr(info, "digits", 2))
    return round(entry_price + offset, getattr(info, "digits", 2))


def _valid_stop_for_side(side: str, entry_price: float, sl: float) -> bool:
    if sl <= 0 or entry_price <= 0:
        return False
    if side == "buy":
        return sl < entry_price
    return sl > entry_price


def _minimum_safe_stop(
    symbol_name: str,
    side: str,
    entry_price: float,
    sl: float,
    spread_points: float,
    min_points: int,
) -> float:
    """Move a valid stop only as far as required by broker/spread constraints."""
    info = mt5.symbol_info(symbol_name)
    point = float(getattr(info, "point", 0.01) or 0.01)
    digits = int(getattr(info, "digits", 2) or 2)
    broker_points = max(
        float(min_points),
        float(getattr(info, "trade_stops_level", 0.0) or 0.0),
        float(getattr(info, "trade_freeze_level", 0.0) or 0.0),
        float(spread_points) * 2.0,
    )
    min_distance = max(point, broker_points * point)
    if side == "buy" and entry_price - sl < min_distance:
        return round(entry_price - min_distance, digits)
    if side == "sell" and sl - entry_price < min_distance:
        return round(entry_price + min_distance, digits)
    return float(sl)


def _optimized_stop_loss(
    symbol_name: str,
    side: str,
    entry_price: float,
    signal_sl: float,
    spread_points: float,
    atr_mult: float,
    min_points: int,
) -> float:
    auto_sl = _auto_stop_loss(symbol_name, side, entry_price, spread_points, atr_mult, min_points)
    if not _valid_stop_for_side(side, entry_price, signal_sl):
        chosen = auto_sl
    elif not _valid_stop_for_side(side, entry_price, auto_sl):
        chosen = float(signal_sl)
    else:
        signal_distance = abs(float(entry_price) - float(signal_sl))
        auto_distance = abs(float(entry_price) - float(auto_sl))
        max_distance = _max_stop_distance(symbol_name)
        if signal_distance <= 0:
            chosen = auto_sl
        elif signal_distance <= max(auto_distance * 2.0, max_distance):
            chosen = float(signal_sl)
        else:
            chosen = auto_sl

    return _cap_stop_distance(symbol_name, side, entry_price, chosen)


def _zone_stop_loss_for_signal(signal: ParsedSignal, entry_price: float, fallback_sl: float, buffer_usd: float) -> float:
    entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
    if len(entries) < 2 or entry_price <= 0:
        return fallback_sl
    if _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0)):
        return fallback_sl
    if signal.side == "buy":
        return round(min(entries) - float(buffer_usd), 2)
    return round(max(entries) + float(buffer_usd), 2)


def _phoenix_range_stop_loss(signal: ParsedSignal, symbol_name: str, entry_price: float, fallback_sl: float) -> float:
    """Derive a Phoenix SL when the provider gives only an entry range.

    The stop sits beyond the far edge of the range, with a small volatility
    allowance proportional to the range width.  A provider SL always wins and
    is handled by the caller before this fallback is used.
    """
    if not _env_bool("PHOENIX_MOMENTUM_RANGE_SL_ENABLED", True):
        return fallback_sl
    entries = sorted(float(value) for value in signal.entries if float(value or 0.0) > 0.0)
    if len(entries) < 2 or entry_price <= 0.0 or _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0)):
        return fallback_sl
    width = max(0.0, entries[-1] - entries[0])
    buffer_usd = max(0.25, _env_float("PHOENIX_RANGE_SL_BUFFER_USD", 1.5))
    allowance = max(buffer_usd, width * 0.25)
    if signal.side == "buy":
        derived = min(entries) - allowance
    else:
        derived = max(entries) + allowance
    info = mt5.symbol_info(symbol_name)
    digits = int(getattr(info, "digits", 2) or 2) if info is not None else 2
    return round(derived, digits)


def _max_stop_distance(symbol_name: str) -> float:
    upper = str(symbol_name or "").upper()
    if "XAU" in upper or "GOLD" in upper:
        return XAU_MAX_STOP_DISTANCE
    if "NAS" in upper or "US100" in upper or "USTEC" in upper:
        return NAS100_MAX_STOP_DISTANCE
    if "US30" in upper or "DJ30" in upper:
        return US30_MAX_STOP_DISTANCE
    if "BTC" in upper:
        return BTC_MAX_STOP_DISTANCE
    return 0.0


def _cap_stop_distance(symbol_name: str, side: str, entry_price: float, sl: float) -> float:
    max_distance = _max_stop_distance(symbol_name)
    if max_distance <= 0.0 or not _valid_stop_for_side(side, entry_price, sl):
        return float(sl)
    distance = abs(float(entry_price) - float(sl))
    if distance <= max_distance:
        return float(sl)
    info = mt5.symbol_info(symbol_name)
    digits = int(getattr(info, "digits", 2) or 2) if info is not None else 2
    capped = float(entry_price) - max_distance if side == "buy" else float(entry_price) + max_distance
    return round(capped, digits)


def _cap_stop_distance_value(symbol_name: str, side: str, entry_price: float, sl: float, max_distance: float) -> float:
    if max_distance <= 0.0 or not _valid_stop_for_side(side, entry_price, sl):
        return float(sl)
    if abs(float(entry_price) - float(sl)) <= max_distance:
        return float(sl)
    info = mt5.symbol_info(symbol_name)
    digits = int(getattr(info, "digits", 2) or 2) if info is not None else 2
    capped = float(entry_price) - max_distance if side == "buy" else float(entry_price) + max_distance
    return round(capped, digits)


def _invalid_levels_reason(side: str, entry_price: float, sl: float, tp: float) -> str | None:
    if side == "buy":
        if tp <= entry_price:
            return "invalid_buy_tp_direction"
        if sl >= entry_price:
            return "invalid_buy_sl_direction"
        return None
    if tp >= entry_price:
        return "invalid_sell_tp_direction"
    if sl <= entry_price:
        return "invalid_sell_sl_direction"
    return None


def _invalid_pending_reason(side: str, order_kind: str, entry_price: float, tick) -> str | None:
    ask = float(tick.ask)
    bid = float(tick.bid)
    if side == "buy" and order_kind == "limit" and entry_price >= ask:
        return "buy_limit_entry_not_below_market"
    if side == "sell" and order_kind == "limit" and entry_price <= bid:
        return "sell_limit_entry_not_above_market"
    if side == "buy" and order_kind == "stop" and entry_price <= ask:
        return "buy_stop_entry_not_above_market"
    if side == "sell" and order_kind == "stop" and entry_price >= bid:
        return "sell_stop_entry_not_below_market"
    return None


def _pending_kind_for_entry(side: str, entry_price: float, tick) -> str:
    if side == "buy":
        return "limit" if entry_price < float(tick.ask) else "stop"
    return "limit" if entry_price > float(tick.bid) else "stop"


def _market_entry_gap_limit(symbol_name: str, spread_points: float, min_points: int) -> float:
    info = mt5.symbol_info(symbol_name)
    point = float(getattr(info, "point", 0.01) or 0.01)
    return max(point * float(min_points) * 4.0, spread_points * point * 3.0)


def _market_reference_price(side: str, tick) -> float:
    return float(tick.ask if side == "buy" else tick.bid)


def _select_execution_tp(tps: list[float], target_index: int) -> float:
    if not tps:
        raise ValueError("Signal has no TP levels")
    index = min(max(1, int(target_index)), len(tps)) - 1
    return float(tps[index])


def _select_live_tps(side: str, entry_price: float, tps: list[float], target_index: int) -> tuple[float, float, int, list[float]]:
    if side == "buy":
        live_tps = [float(tp) for tp in tps if float(tp) > entry_price]
    else:
        live_tps = [float(tp) for tp in tps if float(tp) < entry_price]
    if not live_tps:
        raise ValueError("no_live_tp_remaining")
    live_index = min(max(1, int(target_index)), len(live_tps)) - 1
    return float(live_tps[0]), float(live_tps[live_index]), live_index + 1, live_tps


def _ensure_asset_symbol(asset: str) -> str:
    candidates = ASSET_SYMBOL_CANDIDATES.get(asset, (ASSET_SYMBOLS.get(asset, asset),))
    raw_profile_map = str(os.getenv("SIGNAL_ASSET_SYMBOL_MAP", "") or "").strip()
    if raw_profile_map:
        try:
            profile_map = json.loads(raw_profile_map)
            configured = profile_map.get(asset) if isinstance(profile_map, dict) else None
            if isinstance(configured, str) and configured.strip():
                candidates = (configured.strip(), *candidates)
            elif isinstance(configured, list):
                explicit = tuple(str(item).strip() for item in configured if str(item).strip())
                if explicit:
                    candidates = (*explicit, *candidates)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning(f"[SYMBOL] invalid SIGNAL_ASSET_SYMBOL_MAP: {exc}")
    errors: list[str] = []
    for candidate in dict.fromkeys(candidates):
        try:
            return ensure_symbol(candidate)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(f"Could not resolve MT5 symbol for {asset}: {'; '.join(errors)}")


def _optional_asset_symbol(asset: str) -> str | None:
    try:
        return _ensure_asset_symbol(asset)
    except Exception as exc:
        log.warning(f"[SYMBOL] optional asset unavailable: {asset}: {exc}")
        return None


def _short_signal_id(signal: ParsedSignal) -> str:
    return signal.uid.rsplit(":", 1)[-1][:12]


def _managed_signal_id(signal: ParsedSignal) -> str:
    raw = signal.uid.rsplit(":", 1)[-1]
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "", raw)
    return cleaned[:32] or _short_signal_id(signal)


def _signal_content_signature_values(
    asset: str,
    chat_id: int | None,
    side: str,
    order_kind: str,
    entries: list[float],
    sl: float,
    tps: list[float],
) -> str:
    levels = {
        "asset": asset,
        "chat_id": chat_id,
        "side": side,
        "order_kind": order_kind,
        "entries": [round(float(value), 3) for value in entries],
        "sl": round(float(sl), 3),
        "tps": [round(float(value), 3) for value in tps],
    }
    raw = json.dumps(levels, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _signal_content_signature(signal: ParsedSignal) -> str:
    return _signal_content_signature_values(
        signal.asset,
        signal.chat_id,
        signal.side,
        signal.order_kind,
        signal.entries,
        signal.sl,
        signal.tps,
    )


def _signal_global_content_signature(signal: ParsedSignal) -> str:
    return _signal_content_signature_values(
        signal.asset,
        None,
        signal.side,
        signal.order_kind,
        signal.entries,
        signal.sl,
        signal.tps,
    )


def _relay_content_signature(signal: ParsedSignal) -> str:
    return "relay:" + _signal_global_content_signature(signal)


def _ghp_family_content_signature(signal: ParsedSignal) -> str | None:
    """Deduplicate mirrored Gold Hunter packages across public/VIP feeds."""
    if source_family(signal.chat_id, signal.chat_title) != "ghp_gold":
        return None
    precision = 2 if signal.asset == "gold" else 5
    payload = {
        "family": "ghp_gold",
        "asset": signal.asset,
        "side": signal.side,
        "entries": sorted(round(float(value), precision) for value in signal.entries),
        "sl": round(float(signal.sl), precision),
        "tps": sorted(round(float(value), precision) for value in signal.tps),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "ghp:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _ghp_family_trade_signature(signal: ParsedSignal) -> str | None:
    """Match mirrored GHP trades even when one feed publishes an SL correction."""
    if source_family(signal.chat_id, signal.chat_title) != "ghp_gold":
        return None
    precision = 2 if signal.asset == "gold" else 5
    payload = {
        "family": "ghp_gold_trade",
        "asset": signal.asset,
        "side": signal.side,
        "entries": sorted(round(float(value), precision) for value in signal.entries),
        "tps": sorted(round(float(value), precision) for value in signal.tps),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "ghp-trade:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _signal_global_content_signature_values(
    asset: str,
    side: str,
    order_kind: str,
    entries: list[float],
    sl: float,
    tps: list[float],
) -> str:
    return _signal_content_signature_values(asset, None, side, order_kind, entries, sl, tps)


def _is_cancel_message(text: str) -> bool:
    return pending_cancel_candidate(_normalize_text(text))


def _is_hold_message(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    return bool(HOLD_RE.search(normalized))


def _is_secure_message(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if SECURE_ADVISORY_RE.search(normalized) and not SECURE_IMMEDIATE_RE.search(normalized):
        return False
    return bool(SECURE_RE.search(normalized) or SECURE_STANDALONE_BE_RE.fullmatch(normalized))


def _tp_hit_level(text: str) -> int:
    normalized = _normalize_text(text)
    if not normalized or "tp" not in normalized.lower():
        return 0
    lowered = normalized.lower()
    tp_price_lines = re.findall(r"(?i)\b(?:tp|take\s*profit)\s*[1-9]?\s*[:=\-]?\s*\d{3,}(?:\.\d+)?", normalized)
    explicit_result = bool(re.search(r"\b(?:hit|reached|done|zaliczon\w*|target\s+done|profit\s+book)\b", lowered, re.I))
    # A complete signal often lists several TP prices and may mark the final
    # target as "open✅". That checkmark describes the target type, not a hit.
    if len(tp_price_lines) >= 2 and not explicit_result:
        return 0
    hit_context = bool(re.search(r"(✅|hit|zaliczon|zaliczone|zaliczony|reached|done|boss|profit)", lowered, re.I))
    if "wszystkie" in lowered and hit_context:
        return 99
    if not hit_context:
        return 0
    levels = [int(match.group(1)) for match in TP_HIT_RE.finditer(normalized)]
    return max(levels) if levels else 0


def _price_reached_tp(side: str, current_price: float, tp: float) -> bool:
    if side == "buy":
        return current_price >= tp
    return current_price <= tp


def _pending_expiry_minutes(cfg, managed: dict) -> float:
    override = float(managed.get("pending_expiry_override_minutes", 0.0) or 0.0)
    if override > 0.0:
        return override
    strategy_name = str(managed.get("strategy", "") or "").lower()
    chat_title = str(managed.get("chat_title", "") or "").lower()
    is_phoenix = "phoenix" in strategy_name or "phoenix" in chat_title
    if bool(managed.get("provider_explicit_pending", False)) and (
        is_phoenix
    ):
        return _env_float("PHOENIX_EXPLICIT_PENDING_EXPIRY_MINUTES", 120.0)
    if is_phoenix:
        return _env_float("PHOENIX_PENDING_EXPIRY_MINUTES", 15.0)
    strategy_expiry = float(managed.get("strategy_pending_expiry_minutes", 0.0) or 0.0)
    if strategy_expiry > 0:
        return strategy_expiry
    return min(float(cfg.signal_pending_expiry_minutes), 60.0)


def _pending_price_cancellation_allowed(managed: dict) -> bool:
    """Explicit provider limits wait for activation; pre-entry price cannot count as TP progress."""
    strategy_name = str(managed.get("strategy", "") or "").lower()
    chat_title = str(managed.get("chat_title", "") or "").lower()
    if "phoenix" in strategy_name or "phoenix" in chat_title:
        return False
    return not bool(managed.get("provider_explicit_pending", False))


def _pending_limit_moved_away(
    symbol_name: str,
    managed: dict,
    current_price: float,
    min_points: int,
    age_minutes: float,
    expiry_minutes: float,
) -> tuple[bool, float, float]:
    if str(managed.get("order_kind", "") or "").lower() != "limit":
        return False, 0.0, 0.0
    side = str(managed.get("side", "") or "").lower()
    entry = float(managed.get("entry", 0.0) or 0.0)
    tp1 = float(managed.get("tp1", 0.0) or 0.0)
    if side not in {"buy", "sell"} or entry <= 0 or tp1 <= 0:
        return False, 0.0, 0.0
    info = mt5.symbol_info(symbol_name)
    point = float(getattr(info, "point", 0.01) or 0.01)
    tp_distance = abs(tp1 - entry)
    threshold = max(tp_distance * 2.50, point * float(min_points) * 12.0)
    min_age = min(25.0, max(10.0, float(expiry_minutes) * 0.60))
    moved_away = (current_price - entry) if side == "buy" else (entry - current_price)
    return age_minutes >= min_age and moved_away >= threshold, moved_away, threshold


def _comment_source_tag(signal: ParsedSignal) -> str:
    title = str(signal.chat_title or "").strip().lower()
    explicit = {
        -1002864291293: "phoenixvip",
        -1003576763534: "fxgoldfree",
        -1003669904323: "fxgoldtr",
        -1003825897091: "ictgreen",
        -1001515052582: "xaugoldtr",
        -1001819344275: "saeal",
        -1001150362511: "fxgoldkill",
        -1001365880004: "wolfx",
        -1001838220681: "goldprotame",
        -1002528249483: "goldhunter",
        -1001704634655: "goldhunterfx",
        -1001914224843: "xaugoldsign",
        -1001602855991: "maviafx",
        -1001232813229: "nas100pro",
    }
    if signal.chat_id in explicit:
        return explicit[int(signal.chat_id)]
    if "phoenix" in title:
        return "phoenixvip"
    if "wolf" in title:
        return "wolfx"
    if "mavia" in title:
        return "maviafx"
    if "royal" in title:
        return "royalgold"
    if "saeal" in title:
        return "saeal"
    if "mthri" in title:
        return "mthri"
    if "goldhunter" in title or "gold hunter" in title:
        return "goldhunter"
    if "xauusd gold signal" in title:
        return "xaugoldsign"
    if "pips killer" in title:
        return "pipskiller"
    if "nas100" in title:
        return "nas100pro"
    if "gold pro" in title:
        return "goldpro"
    cleaned = re.sub(r"[^a-z0-9]+", "", title)
    if cleaned:
        return cleaned[:9]
    return str(abs(int(signal.chat_id or 0)))[:9] or "unknown"


def _order_comment(signal: ParsedSignal) -> str:
    return f"tg:{_comment_source_tag(signal)}:{_short_signal_id(signal)[:8]}"[:31]


def _managed_state_defaults(raw: dict | None = None) -> dict:
    state = raw if isinstance(raw, dict) else {}
    state.setdefault("signals", {})
    return state


def _load_managed_state(path: Path) -> dict:
    try:
        return _managed_state_defaults(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return _managed_state_defaults()


def _save_managed_state(path: Path, state: dict) -> None:
    signals = state.get("signals", {})
    if isinstance(signals, dict) and len(signals) > 600:
        items = sorted(signals.items(), key=lambda item: str(item[1].get("created_utc", "")))[-600:]
        state["signals"] = dict(items)
    _write_json(path, state)


def _trigger_price_for_tp1(side: str, entry: float, tp1: float, trigger_pct: float) -> float:
    distance = abs(float(tp1) - float(entry))
    if side == "buy":
        return float(tp1) + (distance * float(trigger_pct))
    return float(tp1) - (distance * float(trigger_pct))


def _price_reached_tp1_trigger(side: str, current_price: float, trigger_price: float) -> bool:
    if side == "buy":
        return current_price >= trigger_price
    return current_price <= trigger_price


def _sl_already_protected(side: str, current_sl: float, tp1: float) -> bool:
    if current_sl <= 0:
        return False
    if side == "buy":
        return current_sl >= tp1
    return current_sl <= tp1


def _better_stop(side: str, current_sl: float, candidate_sl: float) -> float:
    if current_sl <= 0:
        return float(candidate_sl)
    if side == "buy":
        return max(float(current_sl), float(candidate_sl))
    return min(float(current_sl), float(candidate_sl))


def _phoenix_range_be_candidate(
    side: str,
    entry: float,
    current_price: float,
    current_sl: float,
    trigger_usd: float,
    buffer_usd: float,
) -> float | None:
    favorable_move = current_price - entry if side == "buy" else entry - current_price
    if favorable_move < max(0.0, trigger_usd):
        return None
    candidate = entry + buffer_usd if side == "buy" else entry - buffer_usd
    improved = _better_stop(side, current_sl, candidate)
    if current_sl > 0 and abs(improved - current_sl) < 0.01:
        return None
    return improved


def _phoenix_range_pending_levels(
    side: str,
    entry_range: list[float],
    market_price: float,
    minimum_gap: float = 0.0,
) -> list[float]:
    if side not in {"buy", "sell"} or len(entry_range) != 2:
        return []
    low, high = min(float(value) for value in entry_range), max(float(value) for value in entry_range)
    candidates = [low, (low + high) / 2.0, high]
    if side == "buy":
        return [level for level in candidates if level < market_price - minimum_gap]
    return [level for level in candidates if level > market_price + minimum_gap]


def _phoenix_range_market_entry_state(
    side: str,
    market_price: float,
    entry_range: list[float],
    tolerance: float,
    chase_max_usd: float,
) -> tuple[bool, bool]:
    """Return whether a pre-range market leg is usable and whether it is a favorable chase."""
    if side not in {"buy", "sell"} or len(entry_range) != 2:
        return False, False
    low, high = min(float(value) for value in entry_range), max(float(value) for value in entry_range)
    market = float(market_price)
    tolerance = max(0.0, float(tolerance))
    chase_max = max(0.0, float(chase_max_usd))
    standard_usable = market <= high + tolerance if side == "buy" else market >= low - tolerance
    if standard_usable:
        return True, False
    favorable_distance = market - high if side == "buy" else low - market
    favorable_chase = 0.0 < favorable_distance <= chase_max
    return favorable_chase, favorable_chase


def _phoenix_limit_pending_levels(side: str, levels: list[float], max_legs: int) -> list[float]:
    """Keep the deepest retracement orders when a preliminary range is crowded."""
    limit = max(0, int(max_legs))
    if limit == 0:
        return []
    ordered = sorted((float(level) for level in levels), reverse=side == "sell")
    return ordered[:limit]


def _phoenix_confirmed_range_legs(
    market_is_usable: bool,
    market_price: float,
    pending_levels: list[float],
    target_count: int,
    replicate_market_targets: bool,
) -> list[tuple[str, float]]:
    market_leg_count = max(1, int(target_count)) if replicate_market_targets else 1
    market_legs = (
        [("market", float(market_price))] * market_leg_count
        if market_is_usable
        else []
    )
    return [*market_legs, *(("limit", float(level)) for level in pending_levels)]


def _phoenix_provider_stop_with_cap(
    side: str,
    entry: float,
    provider_sl: float,
    max_distance: float,
) -> float:
    if not _valid_stop_for_side(side, entry, provider_sl) or max_distance <= 0.0:
        return float(provider_sl)
    if abs(float(entry) - float(provider_sl)) <= max_distance:
        return float(provider_sl)
    capped = float(entry) - max_distance if side == "buy" else float(entry) + max_distance
    return round(capped, 2)


def _atr_trailing_stop(symbol_name: str, side: str, current_price: float, atr_mult: float) -> float | None:
    try:
        frame = get_rates_df(symbol_name, "M5", 180)
        enriched = enrich(frame)
        atr_value = float(enriched.iloc[-1]["atr14"] or 0.0)
    except Exception:
        atr_value = 0.0
    if atr_value <= 0:
        return None
    info = mt5.symbol_info(symbol_name)
    digits = int(getattr(info, "digits", 2) or 2)
    if side == "buy":
        return round(float(current_price) - (atr_value * max(0.1, float(atr_mult))), digits)
    return round(float(current_price) + (atr_value * max(0.1, float(atr_mult))), digits)


def _dynamic_lot_from_balance(cfg, base_balance: float, current_balance: float) -> tuple[float, int, float]:
    balance_basis = max(0.0, float(current_balance) - float(base_balance))
    increments = int(balance_basis // float(cfg.signal_dynamic_lot_step_usd))
    raw_volume = float(cfg.signal_fixed_lot) + (increments * float(cfg.signal_dynamic_lot_add))
    volume = round(min(float(cfg.signal_dynamic_lot_max), raw_volume), 8)
    return volume, increments, balance_basis


def _phoenix_lot_from_balance(current_balance: float) -> tuple[float, int]:
    base_balance = max(0.0, _env_float("PHOENIX_LOT_BASE_BALANCE_USD", 1000.0))
    base_lot = max(0.01, _env_float("PHOENIX_LOT_BASE_PER_POSITION", 0.10))
    step_usd = max(1.0, _env_float("PHOENIX_LOT_BALANCE_STEP_USD", 500.0))
    step_lot = max(0.0, _env_float("PHOENIX_LOT_STEP_ADD", 0.01))
    max_lot = max(base_lot, _env_float("PHOENIX_LOT_MAX_PER_POSITION", 999.0))
    increments = int(max(0.0, float(current_balance) - base_balance) // step_usd)
    return round(min(max_lot, base_lot + increments * step_lot), 8), increments


def _risk_usd_per_leg(
    balance: float,
    total_risk_pct: float,
    leg_count: int,
    per_leg_risk_pct: float = 0.0,
) -> float:
    """Return the cash risk allocated to one execution leg."""
    if per_leg_risk_pct > 0.0:
        return max(0.0, float(balance)) * float(per_leg_risk_pct) / 100.0
    return (
        max(0.0, float(balance))
        * max(0.0, float(total_risk_pct))
        / 100.0
        / max(1, int(leg_count))
    )


def _signal_per_leg_risk_pct(is_phoenix_signal: bool) -> float:
    """Return the configured balance risk for each independently sized leg."""
    if is_phoenix_signal:
        return max(
            0.0,
            _env_float(
                "PHOENIX_RISK_PCT_PER_LEG",
                _env_float("SIGNAL_RISK_PCT_PER_LEG", 0.0),
            ),
        )
    return max(0.0, _env_float("SIGNAL_RISK_PCT_PER_LEG", 0.0))


def _split_total_volume(total_volume: float, leg_count: int, step: float = 0.01, min_volume: float = 0.01) -> list[float]:
    step = max(0.00000001, float(step))
    requested_legs = max(1, int(leg_count))
    total_steps = max(0, int(round(float(total_volume) / step)))
    min_steps = max(1, int(round(float(min_volume) / step)))
    executable_legs = min(requested_legs, total_steps // min_steps)
    if executable_legs <= 0:
        return []
    base_steps, extra_steps = divmod(total_steps, executable_legs)
    return [round((base_steps + (1 if index < extra_steps else 0)) * step, 8) for index in range(executable_legs)]


def _funded_safe_signal_lot(cfg, account_balance: float) -> float:
    funded_balance = float(cfg.signal_funded_account_balance or 0.0) or float(account_balance or 0.0)
    if funded_balance <= 0.0:
        return float(cfg.signal_fixed_lot)
    return round((funded_balance / 100000.0) * float(cfg.signal_funded_safe_signal_lot_per_100k), 4)


def _equity_safe_compound_base(balance: float, equity: float) -> float:
    balance = float(balance or 0.0)
    equity = float(equity or 0.0)
    if balance <= 0.0:
        return max(0.0, equity)
    if equity <= 0.0:
        return balance
    return min(balance, equity)


def _account_risk_base(account: object) -> float:
    """Return the configured account value used for percentage risk sizing."""
    balance = float(getattr(account, "balance", 0.0) or 0.0)
    equity = float(getattr(account, "equity", 0.0) or 0.0)
    basis = str(os.getenv("POSITION_RISK_BASE", "lower") or "lower").strip().lower()
    if basis == "equity":
        return max(0.0, equity or balance)
    if basis == "balance":
        return max(0.0, balance or equity)
    if balance <= 0.0:
        return max(0.0, equity)
    if equity <= 0.0:
        return balance
    return min(balance, equity)


def _equity_safe_signal_lot(cfg, balance: float, equity: float) -> float:
    compound_base = _equity_safe_compound_base(balance, equity)
    if compound_base <= 0.0:
        return float(cfg.signal_fixed_lot)
    return round((compound_base / 100000.0) * float(cfg.signal_funded_safe_signal_lot_per_100k), 4)


def _managed_comment_signal_token(managed: dict) -> str:
    signal_id = str(managed.get("signal_id", "") or "")
    if not signal_id:
        signal_id = str(managed.get("signal_uid", "") or "").rsplit(":", 1)[-1]
    match = re.match(r"([a-zA-Z0-9]{8,})", signal_id)
    return match.group(1)[:8].lower() if match else ""


def _position_matches_managed(position, managed: dict) -> bool:
    position_ticket = int(getattr(position, "ticket", 0) or 0)
    managed_tickets: set[int] = set()
    for key in ("position_ticket", "order_ticket", "deal_ticket"):
        try:
            ticket = int(managed.get(key, 0) or 0)
        except Exception:
            ticket = 0
        if ticket > 0:
            managed_tickets.add(ticket)
    if managed_tickets:
        return position_ticket > 0 and position_ticket in managed_tickets

    comment = str(getattr(position, "comment", "") or "").lower()
    signal_token = _managed_comment_signal_token(managed)
    if signal_token and signal_token in comment:
        try:
            intended_tp = float(managed.get("execution_tp", 0.0) or 0.0)
            position_tp = float(getattr(position, "tp", 0.0) or 0.0)
            return intended_tp > 0.0 and position_tp > 0.0 and abs(position_tp - intended_tp) < 0.05
        except Exception:
            return False

    # A stable ticket or signal token is authoritative. Falling through to
    # price proximity here can attach a new Phoenix leg to an older signal
    # with similar entry/TP levels and apply the older signal's SL.
    if signal_token:
        return False

    # Compatibility only for old state records created before tickets and
    # signal tokens were persisted.
    try:
        same_side = ("buy" if int(getattr(position, "type", 0)) == mt5.POSITION_TYPE_BUY else "sell") == managed.get("side")
        position_tp = float(getattr(position, "tp", 0.0) or 0.0)
        same_tp = position_tp <= 0.0 or abs(position_tp - float(managed.get("execution_tp", 0.0) or 0.0)) < 0.05
        same_entry = abs(float(getattr(position, "price_open", 0.0) or 0.0) - float(managed.get("entry", 0.0) or 0.0)) < 2.0
        return bool(same_side and same_tp and same_entry)
    except Exception:
        return False


def _manual_exit_reason_after_hold(
    *,
    side: str,
    current_price: float,
    intended_sl: float,
    intended_tp: float,
    broker_sl: float,
    broker_tp: float,
    deferred_channel_tp_hit: bool,
) -> str:
    if deferred_channel_tp_hit:
        return "channel_tp"
    if broker_tp <= 0.0 and intended_tp > 0.0 and _price_reached_tp(side, current_price, intended_tp):
        return "tp"
    hit_unarmed_sl = intended_sl > 0.0 and (
        current_price <= intended_sl if side == "buy" else current_price >= intended_sl
    )
    if broker_sl <= 0.0 and hit_unarmed_sl:
        return "sl"
    return ""


def run() -> None:
    cfg = load_settings()
    if cfg.telegram_api_id <= 0:
        raise ValueError("TELEGRAM_API_ID is missing")
    if not cfg.telegram_api_hash:
        raise ValueError("TELEGRAM_API_HASH is missing")
    if not cfg.telegram_watch_channels:
        raise ValueError("WATCH_CHANNELS is missing")

    state_path = cfg.data_dir / "telegram_signal_state.json"
    managed_path = cfg.data_dir / "telegram_signal_managed.json"
    events_path = cfg.data_dir / "telegram_signal_events.jsonl"
    orders_path = cfg.data_dir / "telegram_signal_orders.jsonl"
    channel_analysis_path = cfg.data_dir / "channel_analysis.json"
    session_path = str((cfg.data_dir / cfg.telegram_session_name).resolve())

    state = _load_state(state_path)
    managed_state = _load_managed_state(managed_path)
    processed = set(state.get("processed", []))
    review_agent_enabled = _env_bool("SIGNAL_REVIEW_AGENT_ENABLED", True)
    review_policy = load_review_policy(os.getenv("SIGNAL_REVIEW_POLICY_PATH", ""))
    watch_raw_ids, watch_abs_ids, watch_usernames = _normalize_watch_targets(cfg.telegram_watch_channels)
    trade_raw_ids, trade_abs_ids, trade_usernames = _normalize_watch_targets(cfg.telegram_trade_channels)

    connect(
        Mt5Credentials(
            login=cfg.mt5_login,
            password=cfg.mt5_password,
            server=cfg.mt5_server,
            path=cfg.mt5_path,
        )
    )
    symbol_by_asset = {
        "gold": ensure_symbol(cfg.symbol),
        "nas100": _optional_asset_symbol("nas100"),
        "us30": _optional_asset_symbol("us30"),
        "btc": _optional_asset_symbol("btc"),
    }
    symbol_upper = symbol_by_asset["gold"].upper()
    if "XAU" not in symbol_upper and "GOLD" not in symbol_upper:
        raise RuntimeError(f"Resolved gold symbol is not gold-like: {symbol_by_asset['gold']}")
    session_start_account = account_info()
    session_start_balance = float(getattr(session_start_account, "balance", 0.0) or 0.0)
    session_start_equity = float(getattr(session_start_account, "equity", 0.0) or session_start_balance)
    if (
        (cfg.signal_dynamic_lot_enabled or cfg.signal_lot_mode == "profit_dynamic")
        and cfg.signal_lot_mode not in {"funded_safe", "equity_safe"}
        and float(state.get("dynamic_lot_base_balance", 0.0) or 0.0) <= 0
    ):
        state["dynamic_lot_base_balance"] = session_start_balance
        state["dynamic_lot_base_utc"] = datetime.now(UTC).isoformat()
        _save_state(state_path, state)

    log.info("=" * 70)
    log.info("XAUUSD/NAS100/US30/BTCUSD Telegram Signal Bot")
    log.info("Telegram session: hidden")
    log.info(f"Watching channels: {len(cfg.telegram_watch_channels)} configured")
    log.info(f"Trading channels: {len(cfg.telegram_trade_channels)} configured")
    if cfg.channel_lot_sizes:
        configured_lots = ", ".join(f"{channel}={lot:.2f}" for channel, lot in cfg.channel_lot_sizes.items())
        log.info(f"Channel lot overrides per position: {configured_lots}")
    if _env_bool("PHOENIX_BALANCE_LOT_SCALING_ENABLED", False):
        phoenix_start_lot, phoenix_start_steps = _phoenix_lot_from_balance(session_start_balance)
        log.info(
            f"PHOENIX balance-scaled lot per position: balance={session_start_balance:.2f} "
            f"lot={phoenix_start_lot:.2f} steps={phoenix_start_steps} "
            f"(+{_env_float('PHOENIX_LOT_STEP_ADD', 0.01):.2f} per "
            f"{_env_float('PHOENIX_LOT_BALANCE_STEP_USD', 500.0):.0f} USD above "
            f"{_env_float('PHOENIX_LOT_BASE_BALANCE_USD', 1000.0):.0f} USD)"
        )
    log.info(
        "PHOENIX execution profile: "
        f"entry_mapping={str(os.getenv('PHOENIX_ENTRY_TARGET_MAPPING', 'legacy') or 'legacy').strip().lower()} "
        f"targets={list(PHOENIX_STRATEGY.split_target_indices)} "
        f"protect={str(os.getenv('PHOENIX_SPLIT_PROTECT_MODES', '') or 'strategy-default')} "
        f"provider_sl={'original' if _env_float('PHOENIX_PROVIDER_SL_MAX_DISTANCE_USD', 12.0) <= 0 else 'capped'} "
        f"pending={_env_float('PHOENIX_PENDING_EXPIRY_MINUTES', PHOENIX_STRATEGY.pending_expiry_minutes):.0f}m "
        f"explicit_pending={_env_float('PHOENIX_EXPLICIT_PENDING_EXPIRY_MINUTES', 120.0):.0f}m"
    )
    direction_runner_lot = _env_float("PHOENIX_DIRECTION_RUNNER_LOT", 0.01)
    if _env_bool("PHOENIX_DIRECTION_RUNNER_BALANCE_SCALING_ENABLED", False):
        direction_runner_lot, _direction_start_steps = _phoenix_lot_from_balance(session_start_balance)
    log.info(
        "PHOENIX pre-signal profile: "
        f"enabled={_env_bool('PHOENIX_DIRECTION_RUNNER_ENABLED', False)} "
        f"confirmation={str(os.getenv('PHOENIX_DIRECTION_RUNNER_CONFIRMATION', 'none') or 'none')} "
        f"fallback_lot={direction_runner_lot:.2f} "
        f"risk_per_leg={_env_float('PHOENIX_DIRECTION_RUNNER_RISK_PCT', 0.0):.2f}% "
        f"balance_scaling={_env_bool('PHOENIX_DIRECTION_RUNNER_BALANCE_SCALING_ENABLED', False)} "
        f"tp={_env_float('PHOENIX_DIRECTION_RUNNER_TP_USD', 1.0):.2f}USD "
        f"sl={_env_float('PHOENIX_DIRECTION_RUNNER_SL_USD', 6.0):.2f}USD "
        f"hard_timeout={_env_float('PHOENIX_DIRECTION_RUNNER_MAX_HOLD_MINUTES', 15.0):.0f}m "
        f"range_tolerance={_env_float('PHOENIX_RANGE_TRIGGER_TOLERANCE_USD', 0.5):.2f}USD "
        f"range_entries={_env_bool('PHOENIX_RANGE_TRIGGER_ENABLED', False)}"
    )
    log.info(
        "PHOENIX risk profile: "
        f"full_per_leg={_env_float('PHOENIX_RISK_PCT_PER_LEG', 0.0):.2f}% "
        f"range_per_leg={_env_float('PHOENIX_RANGE_RISK_PCT_PER_LEG', 0.0):.2f}% "
        f"direction_per_leg={_env_float('PHOENIX_DIRECTION_RUNNER_RISK_PCT', 0.0):.2f}%"
    )
    log.info(
        f"Resolved symbols: gold={symbol_by_asset['gold']}, "
        f"nas100={symbol_by_asset['nas100']}, us30={symbol_by_asset['us30']}, btc={symbol_by_asset['btc']}"
    )
    log.info("MT5 account: hidden")
    if (
        cfg.signal_lot_mode == "risk_pct"
        or _env_float("SIGNAL_RISK_PCT", 0.0) > 0.0
        or _env_float("SIGNAL_RISK_PCT_PER_LEG", 0.0) > 0.0
        or _env_float("PHOENIX_SIGNAL_RISK_PCT", 0.0) > 0.0
        or _env_float("PHOENIX_RISK_PCT_PER_LEG", 0.0) > 0.0
    ):
        log.info(
            f"Risk-percent signal sizing: total_risk={max(0.0, _env_float('SIGNAL_RISK_PCT', 0.0)):.2f}% "
            f"per_leg={max(0.0, _env_float('SIGNAL_RISK_PCT_PER_LEG', 0.0)):.2f}% "
            f"of current {str(os.getenv('POSITION_RISK_BASE', 'lower') or 'lower').strip().lower()} basis"
        )
        log.info(
            f"PHOENIX risk override: total_risk="
            f"{max(0.0, _env_float('PHOENIX_SIGNAL_RISK_PCT', _env_float('SIGNAL_RISK_PCT', 0.0))):.2f}% "
            f"per_leg={_signal_per_leg_risk_pct(True):.2f}% of current "
            f"{str(os.getenv('POSITION_RISK_BASE', 'lower') or 'lower').strip().lower()} basis"
        )
        if _env_bool("PHOENIX_EXTRA_TP_RUNNER_ENABLED", False):
            extra_targets = _env_target_tuple(
                "PHOENIX_EXTRA_TP_RUNNER_TARGETS",
                (max(1, int(_env_float("PHOENIX_EXTRA_TP_RUNNER_TARGET_INDEX", 6.0))),),
            )
            extra_protects = tuple(
                value.strip().lower()
                for value in str(
                    os.getenv("PHOENIX_EXTRA_TP_RUNNER_PROTECT_MODES", "") or ""
                ).split(",")
                if value.strip()
            )
            log.info(
                f"PHOENIX extra MARKET runners: targets={list(extra_targets)} "
                f"protect={list(extra_protects)} "
                f"gate={str(os.getenv('PHOENIX_EXTRA_TP_RUNNER_GATE', 'strict_zone') or 'strict_zone')} "
                f"risk_multiplier={max(0.0, _env_float('PHOENIX_EXTRA_TP_RUNNER_RISK_MULTIPLIER', 1.0)):.2f}"
            )
    elif cfg.signal_lot_mode == "equity_safe":
        compound_base = _equity_safe_compound_base(session_start_balance, session_start_equity)
        log.info(
            f"Equity safe compound lot: balance={session_start_balance:.2f}, equity={session_start_equity:.2f}, "
            f"compound_base={compound_base:.2f}, max_signal_lot={_equity_safe_signal_lot(cfg, session_start_balance, session_start_equity):.2f}, "
            f"split=3 legs, skip_below_min={cfg.signal_funded_skip_below_min}"
        )
    elif cfg.signal_lot_mode == "funded_safe":
        funded_balance = float(cfg.signal_funded_account_balance or 0.0) or session_start_balance
        log.info(
            f"Funded safe lot: account={funded_balance:.2f}, "
            f"max_signal_lot={_funded_safe_signal_lot(cfg, funded_balance):.2f}, "
            f"split=3 legs, skip_below_min={cfg.signal_funded_skip_below_min}"
        )
    elif cfg.signal_lot_mode == "profit_dynamic" or cfg.signal_dynamic_lot_enabled:
        startup_dynamic_lot, startup_dynamic_steps, startup_balance_basis = _dynamic_lot_from_balance(
            cfg,
            float(state.get("dynamic_lot_base_balance", session_start_balance) or session_start_balance),
            session_start_balance,
        )
        lot_scope = "position" if _env_bool("SIGNAL_DYNAMIC_LOT_PER_POSITION", True) else "signal"
        log.info(
            f"Profit dynamic lot per {lot_scope} from current balance: lot={startup_dynamic_lot:.2f}, "
            f"steps={startup_dynamic_steps}, balance_basis={startup_balance_basis:.2f}, "
            f"+{cfg.signal_dynamic_lot_add:.2f} lot per {cfg.signal_dynamic_lot_step_usd:.2f} USD, "
            f"min={cfg.signal_fixed_lot:.2f}, max={cfg.signal_dynamic_lot_max:.2f}"
        )
    else:
        log.info(f"Fixed lot: {cfg.signal_fixed_lot:.2f}")
    if cfg.signal_session_net_profit_stop_usd > 0:
        log.info(f"Session net profit stop: +{cfg.signal_session_net_profit_stop_usd:.2f} USD from start balance")
    if cfg.signal_session_net_loss_stop_usd > 0:
        log.info(f"Session net loss stop: -{cfg.signal_session_net_loss_stop_usd:.2f} USD from start equity")
    log.info(f"Pending expiry: {cfg.signal_pending_expiry_minutes:.0f} minutes")
    if cfg.signal_adaptive_learning_enabled:
        if _env_bool("SIGNAL_ADAPTIVE_ALLOW_CHANNEL_PAUSE", False):
            log.info("Adaptive learning: ON, rolling trade audit with automatic channel pause enabled")
        else:
            log.info("Adaptive learning: ON in observation mode; parsers and outcomes logged without pausing channels")
    else:
        log.info("Adaptive learning: OFF for live trading; run strategy analysis after gold market close")
    log.info("=" * 70)

    client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)

    async def _start_client() -> None:
        await client.start(
            phone=(lambda: cfg.telegram_phone or input("Telegram phone: ").strip()),
            password=(lambda: cfg.telegram_2fa_password or input("Telegram 2FA password: ").strip()),
        )

    def _matches_channel(chat_id: int | None, username: str | None) -> bool:
        if chat_id is not None and (chat_id in watch_raw_ids or abs(chat_id) in watch_abs_ids):
            return True
        if username and username.lower() in watch_usernames:
            return True
        return False

    def _matches_trade_channel(chat_id: int | None, username: str | None) -> bool:
        if chat_id is not None and (chat_id in trade_raw_ids or abs(chat_id) in trade_abs_ids):
            return True
        if username and username.lower() in trade_usernames:
            return True
        return False

    def _matches_sender(sender_id: int | None, post_author: str) -> bool:
        if cfg.telegram_allowed_sender_ids and sender_id is not None:
            if int(sender_id) in cfg.telegram_allowed_sender_ids:
                return True
        if cfg.telegram_allowed_post_authors and post_author:
            if post_author.lower() in {item.lower() for item in cfg.telegram_allowed_post_authors}:
                return True
        return not cfg.telegram_allowed_sender_ids and not cfg.telegram_allowed_post_authors

    def _save_runtime_state() -> None:
        state["processed"] = sorted(processed)
        _save_state(state_path, state)

    phoenix_poll_fingerprints = {
        str(key): str(value)
        for key, value in (state.get("phoenix_poll_fingerprints", {}) or {}).items()
        if str(key).isdigit() and value
    }
    channel_poll_fingerprints = {
        str(key): str(value)
        for key, value in (state.get("channel_poll_fingerprints", {}) or {}).items()
        if ":" in str(key) and value
    }

    def _remember_phoenix_poll_fingerprint(message_id: int, text: str) -> None:
        if not message_id:
            return
        fingerprint = hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()
        phoenix_poll_fingerprints[str(int(message_id))] = fingerprint
        if len(phoenix_poll_fingerprints) > 250:
            newest_ids = sorted((int(key) for key in phoenix_poll_fingerprints), reverse=True)[:200]
            keep = {str(message_id) for message_id in newest_ids}
            for key in list(phoenix_poll_fingerprints):
                if key not in keep:
                    phoenix_poll_fingerprints.pop(key, None)
        state["phoenix_poll_fingerprints"] = dict(phoenix_poll_fingerprints)
        _save_runtime_state()

    def _remember_channel_poll_fingerprint(chat_id: int, message_id: int, text: str) -> None:
        if not chat_id or not message_id:
            return
        key = f"{int(chat_id)}:{int(message_id)}"
        channel_poll_fingerprints[key] = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
        if len(channel_poll_fingerprints) > 1500:
            for old_key in list(channel_poll_fingerprints)[:500]:
                channel_poll_fingerprints.pop(old_key, None)
        state["channel_poll_fingerprints"] = dict(channel_poll_fingerprints)
        _save_runtime_state()

    def _remember_phoenix_direction(chat_id: int | None, side: str, message_id: int) -> None:
        hints = state.setdefault("phoenix_direction_hints", {})
        hints[str(int(chat_id or 0))] = {
            "side": side,
            "message_id": int(message_id or 0),
            "seen_utc": datetime.now(UTC).isoformat(),
        }
        _save_runtime_state()

    def _fresh_phoenix_direction(chat_id: int | None) -> str | None:
        hints = state.get("phoenix_direction_hints", {})
        hint = hints.get(str(int(chat_id or 0)), {}) if isinstance(hints, dict) else {}
        try:
            seen = datetime.fromisoformat(str(hint.get("seen_utc", "")).replace("Z", "+00:00"))
        except Exception:
            return None
        ttl_minutes = max(1.0, _env_float("PHOENIX_DIRECTION_HINT_TTL_MINUTES", 20.0))
        if datetime.now(UTC) - seen > timedelta(minutes=ttl_minutes):
            return None
        side = str(hint.get("side", "")).lower()
        return side if side in {"buy", "sell"} else None

    def _consume_phoenix_direction(chat_id: int | None) -> None:
        hints = state.get("phoenix_direction_hints", {})
        if isinstance(hints, dict) and hints.pop(str(int(chat_id or 0)), None) is not None:
            _save_runtime_state()

    def _ghp_contextual_signal_text(
        chat_id: int | None,
        chat_title: str,
        message_id: int,
        text: str,
    ) -> str:
        """Rebuild GHP signals published as direction/entry/SL/TP fragments."""
        if not is_ghp_source(chat_id, chat_title):
            return text
        now = datetime.now(UTC)
        ttl = timedelta(minutes=max(2.0, _env_float("GHP_CONTEXT_TTL_MINUTES", 15.0)))
        contexts = state.setdefault("ghp_parser_context", {})
        key = str(int(chat_id or 0))
        records = contexts.setdefault(key, [])
        records = [
            item
            for item in records
            if item.get("seen_utc")
            and now - datetime.fromisoformat(str(item["seen_utc"]).replace("Z", "+00:00")) <= ttl
            and int(item.get("message_id", 0) or 0) != int(message_id or 0)
        ]
        direct = parse_ghp_message(text, chat_title)
        market_entry = 0.0
        if direct.side in {"buy", "sell"} and direct.asset:
            try:
                context_symbol = symbol_by_asset.get(direct.asset) or _optional_asset_symbol(direct.asset)
                if context_symbol:
                    symbol_by_asset[direct.asset] = context_symbol
                    market_entry = _market_reference_price(direct.side, get_tick(context_symbol))
            except Exception as exc:
                log.warning(f"[GHP PARSER] market context unavailable for {direct.asset}: {exc}")
        records.append(
            {
                "message_id": int(message_id or 0),
                "seen_utc": now.isoformat(),
                "text": str(text or "")[:3000],
                "kind": direct.kind,
                "asset": direct.asset,
                "side": direct.side,
                "market_entry": float(market_entry),
            }
        )
        records = records[-8:]
        contexts[key] = records
        state["ghp_parser_context"] = contexts
        _save_runtime_state()

        if direct.kind == "signal":
            return text
        if direct.kind == "add":
            for previous in reversed(records[:-1]):
                previous_message = parse_ghp_message(str(previous.get("text", "")), chat_title)
                previous_signal = previous_message.signal
                if previous_message.kind != "signal" or previous_signal is None:
                    continue
                if direct.side and previous_signal.side != direct.side:
                    continue
                try:
                    reentry_symbol = symbol_by_asset.get(previous_signal.asset) or _optional_asset_symbol(previous_signal.asset)
                    if not reentry_symbol:
                        continue
                    symbol_by_asset[previous_signal.asset] = reentry_symbol
                    reentry_price = _market_reference_price(previous_signal.side, get_tick(reentry_symbol))
                except Exception as exc:
                    log.warning(f"[GHP PARSER] re-entry market unavailable for {previous_signal.asset}: {exc}")
                    continue
                if not _valid_stop_for_side(previous_signal.side, reentry_price, previous_signal.sl):
                    continue
                live_tps = [
                    float(tp)
                    for tp in previous_signal.tps
                    if (previous_signal.side == "buy" and float(tp) > reentry_price)
                    or (previous_signal.side == "sell" and float(tp) < reentry_price)
                ]
                if not live_tps:
                    continue
                symbol_token = previous_signal.symbol_token or (
                    "XAUUSD" if previous_signal.asset == "gold" else previous_signal.asset.upper()
                )
                reconstructed = (
                    f"{symbol_token} {previous_signal.side.upper()} NOW ENTRY {reentry_price:.6f} "
                    f"SL {previous_signal.sl:.6f} "
                    + " ".join(f"TP{index} {tp:.6f}" for index, tp in enumerate(live_tps, start=1))
                )
                if parse_ghp_message(reconstructed, chat_title).kind == "signal":
                    _append_jsonl(
                        events_path,
                        {
                            "type": "ghp_context_reentry_reconstructed",
                            "chat_id": int(chat_id or 0),
                            "message_id": int(message_id or 0),
                            "source_message_id": int(previous.get("message_id", 0) or 0),
                            "text": reconstructed,
                        },
                    )
                    log.info(
                        f"[GHP PARSER] reconstructed provider re-entry from message "
                        f"{int(previous.get('message_id', 0) or 0)}"
                    )
                    return reconstructed
            return text
        if direct.kind not in {"commentary", "direction", "pre_signal"}:
            return text
        if _looks_like_complete_provider_signal_text(text):
            _append_jsonl(
                events_path,
                {
                    "type": "ghp_context_invalid_complete_signal_not_merged",
                    "chat_id": int(chat_id or 0),
                    "message_id": int(message_id or 0),
                    "text": str(text or "")[:2000],
                },
            )
            log.warning(
                f"[GHP PARSER] complete-looking message={int(message_id or 0)} is invalid; "
                "not merging it with an older signal"
            )
            return text
        structural = re.compile(
            r"\b(?:entry|zone|focus|sl|s/l|stop\s*loss|tp\s*\d*|targets?|take\s*profit|buy|sell)\b",
            re.I,
        )
        if not structural.search(text) or not re.search(r"\d", text):
            return text
        for width in range(2, min(6, len(records)) + 1):
            selected = records[-width:]
            combined = "\n".join(str(item.get("text", "")) for item in selected)
            selected_sides = {
                str(item.get("side", ""))
                for item in selected
                if str(item.get("side", "")) in {"buy", "sell"}
            }
            if len(selected_sides) > 1:
                continue
            reconstructed = parse_ghp_message(combined, chat_title)
            if reconstructed.kind != "signal":
                fragment_side = next(
                    (str(item.get("side", "")) for item in selected if str(item.get("side", "")) in {"buy", "sell"}),
                    "",
                )
                has_sl = bool(re.search(r"\b(?:sl|s/l|stop\s*loss)\b\s*[:=@-]?\s*\d", combined, re.I))
                has_tp = bool(re.search(r"\b(?:tp\s*\d*|targets?|take\s*profit)\b\s*[:=@-]?\s*\d", combined, re.I))
                if fragment_side and has_sl and has_tp:
                    entry_hint = next(
                        (
                            float(item.get("market_entry", 0.0) or 0.0)
                            for item in selected
                            if str(item.get("side", "")) == fragment_side
                            and float(item.get("market_entry", 0.0) or 0.0) > 0.0
                        ),
                        0.0,
                    )
                    if entry_hint > 0.0:
                        combined = f"{combined}\nENTRY {entry_hint:.6f}"
                        reconstructed = parse_ghp_message(combined, chat_title)
            if reconstructed.kind == "signal":
                _append_jsonl(
                    events_path,
                    {
                        "type": "ghp_context_signal_reconstructed",
                        "chat_id": int(chat_id or 0),
                        "message_id": int(message_id or 0),
                        "source_message_ids": [
                            int(item.get("message_id", 0) or 0) for item in records[-width:]
                        ],
                        "text": combined,
                    },
                )
                log.info(
                    f"[GHP PARSER] reconstructed fragmented signal from {width} messages "
                    f"ending at {int(message_id or 0)}"
                )
                contexts[key] = []
                state["ghp_parser_context"] = contexts
                _save_runtime_state()
                return combined
        return text

    def _open_phoenix_direction_runner(chat_id: int | None, chat_title: str, message_id: int, side: str) -> bool:
        if not _env_bool("PHOENIX_DIRECTION_RUNNER_ENABLED", True):
            return False
        active_hours = _env_int_set("PHOENIX_DIRECTION_RUNNER_ACTIVE_UTC_HOURS")
        current_hour = datetime.now(UTC).hour
        if active_hours and current_hour not in active_hours:
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_direction_runner_session_skip",
                    "chat_id": int(chat_id or 0),
                    "chat_title": chat_title,
                    "message_id": int(message_id or 0),
                    "side": side,
                    "current_utc_hour": current_hour,
                    "active_utc_hours": sorted(active_hours),
                },
            )
            log.info(
                f"[PHOENIX DIR RUNNER] skipped outside tested UTC hours: "
                f"hour={current_hour:02d} active={sorted(active_hours)}"
            )
            return False
        if _env_bool("PHOENIX_DIRECTION_RUNNER_REQUIRE_RANGE", False):
            log.info(
                f"[PHOENIX DIR RUNNER] armed {side.upper()}; "
                "waiting for numeric range confirmation"
            )
            return False
        key = f"{int(chat_id or 0)}:{int(message_id or 0)}"
        opened_messages = state.setdefault("phoenix_direction_runner_messages", [])
        if key in opened_messages:
            return False
        symbol_name = symbol_by_asset["gold"]
        confirmation_mode = str(
            os.getenv("PHOENIX_DIRECTION_RUNNER_CONFIRMATION", "none") or "none"
        ).strip().lower()
        if confirmation_mode == "pullback":
            try:
                frame = get_rates_df(symbol_name, "M1", 40)
                closed = frame.iloc[:-1] if len(frame) > 1 else frame.iloc[0:0]
                closes = [float(value) for value in closed["close"].tolist()]
                confirmed = _phoenix_direction_pullback_confirmed(side, closes)
            except Exception as exc:
                confirmed = False
                log.warning(f"[PHOENIX PRE CONFIRM] M1 confirmation failed: {exc}")
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_direction_confirmation",
                    "chat_id": int(chat_id or 0),
                    "message_id": int(message_id or 0),
                    "side": side,
                    "mode": confirmation_mode,
                    "confirmed": confirmed,
                },
            )
            if not confirmed:
                log.info(
                    f"[PHOENIX PRE CONFIRM] {side.upper()} not confirmed by EMA9/21 pullback; "
                    "waiting for numeric range"
                )
                return False
        tick = get_tick(symbol_name)
        entry = float(tick.ask if side == "buy" else tick.bid)
        sl_distance = max(0.1, _env_float("PHOENIX_DIRECTION_RUNNER_SL_USD", 3.0))
        tp_distance = max(0.1, _env_float("PHOENIX_DIRECTION_RUNNER_TP_USD", 1.5))
        sl = entry - sl_distance if side == "buy" else entry + sl_distance
        tp = entry + tp_distance if side == "buy" else entry - tp_distance
        spread_points = current_spread_points(symbol_name)
        sl = _minimum_safe_stop(symbol_name, side, entry, sl, spread_points, cfg.signal_sl_min_points)
        requested_volume = _env_float("PHOENIX_DIRECTION_RUNNER_LOT", 1.0)
        direction_lot_steps = 0
        direction_risk_pct = max(0.0, _env_float("PHOENIX_DIRECTION_RUNNER_RISK_PCT", 0.0))
        if direction_risk_pct > 0.0:
            current_balance = _account_risk_base(account_info())
            loss_per_lot = calc_loss_per_lot(symbol_name, side, entry, sl)
            if loss_per_lot > 0.0:
                requested_volume = _risk_usd_per_leg(
                    current_balance,
                    0.0,
                    1,
                    direction_risk_pct,
                ) / loss_per_lot
        elif _env_bool("PHOENIX_DIRECTION_RUNNER_BALANCE_SCALING_ENABLED", False):
            current_balance = float(getattr(account_info(), "balance", 0.0) or 0.0)
            requested_volume, direction_lot_steps = _phoenix_lot_from_balance(current_balance)
        volume = normalize_volume(
            symbol_name,
            requested_volume,
            cfg.min_lot,
            max(cfg.max_lot, cfg.signal_dynamic_lot_max),
        )
        result = send_market_order(
            symbol=symbol_name,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            deviation=cfg.deviation,
            magic=cfg.magic,
            comment="PHOENIX:DIR RUNNER",
        )
        retcode = getattr(result, "retcode", None)
        position_ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
        success = retcode in {10008, 10009}
        _append_jsonl(
            events_path,
            {
                "type": "phoenix_direction_runner_attempt",
                "chat_id": chat_id,
                "chat_title": chat_title,
                "message_id": message_id,
                "side": side,
                "symbol": symbol_name,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "volume": volume,
                "risk_pct_per_leg": direction_risk_pct,
                "balance_scaling_steps": direction_lot_steps,
                "position_ticket": position_ticket,
                "retcode": retcode,
            },
        )
        if success:
            opened_messages.append(key)
            state["phoenix_direction_runner_messages"] = opened_messages[-250:]
            if position_ticket:
                runners = state.setdefault("phoenix_direction_runner_positions", {})
                runners[str(position_ticket)] = {
                    "opened_utc": datetime.now(UTC).isoformat(),
                    "chat_id": int(chat_id or 0),
                    "message_id": int(message_id or 0),
                    "side": side,
                    "entry": entry,
                }
            _save_runtime_state()
            log.info(
                f"[PHOENIX DIR RUNNER] opened confirmed {side.upper()} MARKET volume={volume:.2f} "
                f"entry={entry:.2f} sl={sl:.2f} tp={tp:.2f}"
            )
        else:
            log.warning(f"[PHOENIX DIR RUNNER] order rejected: side={side} retcode={retcode}")
        return success

    def _open_phoenix_range_trigger(
        chat_id: int | None,
        chat_title: str,
        message_id: int,
        side: str,
        entry_range: list[float],
    ) -> bool:
        if not _env_bool("PHOENIX_RANGE_TRIGGER_ENABLED", True) or len(entry_range) != 2:
            return False
        key = f"{int(chat_id or 0)}:{int(message_id or 0)}"
        opened_messages = state.setdefault("phoenix_range_trigger_messages", [])
        if key in opened_messages:
            return False
        symbol_name = symbol_by_asset["gold"]
        tick = get_tick(symbol_name)
        entry = float(tick.ask if side == "buy" else tick.bid)
        low, high = min(entry_range), max(entry_range)
        tolerance = max(0.0, _env_float("PHOENIX_RANGE_TRIGGER_TOLERANCE_USD", 2.0))
        market_is_usable, market_is_chase = _phoenix_range_market_entry_state(
            side,
            entry,
            [low, high],
            tolerance,
            _env_float("PHOENIX_RANGE_MARKET_CHASE_MAX_USD", 0.0),
        )
        always_stage = _env_bool("PHOENIX_ALWAYS_STAGE_RANGE_PENDING", False)
        if not market_is_usable and not always_stage:
            log.info(
                f"[PHOENIX-RANGE] skipped wrong-side market: side={side.upper()} "
                f"market={entry:.2f} range={low:.2f}-{high:.2f} tolerance={tolerance:.2f}"
            )
            return False
        target_distances = []
        for raw_target in str(os.getenv("PHOENIX_RANGE_TRIGGER_TPS_USD", "1,2,3") or "1,2,3").split(","):
            try:
                target_distances.append(max(0.1, float(raw_target.strip())))
            except ValueError:
                continue
        target_distances = target_distances or [1.0, 2.0, 3.0]
        sl_distance = max(max(target_distances), _env_float("PHOENIX_RANGE_TRIGGER_SL_USD", 6.0))
        spread_points = current_spread_points(symbol_name)
        pending_levels = []
        if _env_bool("PHOENIX_RANGE_STAGED_PENDING_ENABLED", True):
            pending_levels = _phoenix_range_pending_levels(side, [low, high], entry, minimum_gap=0.01)
            pending_levels = _phoenix_limit_pending_levels(
                side,
                pending_levels,
                int(_env_float("PHOENIX_RANGE_MAX_PENDING_LEGS", 3.0)),
            )
        planned_legs = _phoenix_confirmed_range_legs(
            market_is_usable=market_is_usable,
            market_price=entry,
            pending_levels=pending_levels,
            target_count=len(target_distances),
            replicate_market_targets=_env_bool("PHOENIX_RANGE_REPLICATE_MARKET_TARGETS", False),
        )
        if not planned_legs:
            log.info(
                f"[PHOENIX-RANGE] no executable range legs: side={side.upper()} "
                f"market={entry:.2f} range={low:.2f}-{high:.2f}"
            )
            return False
        if not market_is_usable:
            log.info(
                f"[PHOENIX-RANGE] market already beyond range; staging {len(planned_legs)} pending legs: "
                f"side={side.upper()} market={entry:.2f} range={low:.2f}-{high:.2f}"
            )
        balance = _account_risk_base(account_info())
        risk_pct = max(0.0, _env_float("PHOENIX_RANGE_TRIGGER_RISK_PCT", 1.0))
        range_per_leg_risk_pct = max(0.0, _env_float("PHOENIX_RANGE_RISK_PCT_PER_LEG", 0.0))
        risk_usd = _risk_usd_per_leg(
            balance,
            risk_pct,
            len(planned_legs),
            range_per_leg_risk_pct,
        )
        max_volume = max(cfg.min_lot, _env_float("PHOENIX_RANGE_TRIGGER_MAX_LOT", 0.20))
        fixed_volume = _env_float("PHOENIX_RANGE_TRIGGER_FIXED_LOT", 0.0)
        range_balance_scaling = _env_bool(
            "PHOENIX_RANGE_BALANCE_LOT_SCALING_ENABLED",
            _env_bool("PHOENIX_BALANCE_LOT_SCALING_ENABLED", False),
        )
        if range_balance_scaling:
            fixed_volume, scaling_steps = _phoenix_lot_from_balance(balance)
            log.info(
                f"[LOT] PHOENIX range balance scaling: balance={balance:.2f} "
                f"steps={scaling_steps} lot_per_leg={fixed_volume:.2f}"
            )
        successful_legs = 0
        pending_records = state.setdefault("phoenix_range_pending_orders", [])
        expiry_minutes = max(1.0, _env_float("PHOENIX_RANGE_PENDING_EXPIRY_MINUTES", 15.0))
        created_utc = datetime.now(UTC)
        for leg_index, (order_kind, leg_entry) in enumerate(planned_legs, start=1):
            target_index = min(leg_index - 1, len(target_distances) - 1)
            tp_distance = target_distances[target_index]
            sl = leg_entry - sl_distance if side == "buy" else leg_entry + sl_distance
            sl = _minimum_safe_stop(symbol_name, side, leg_entry, sl, spread_points, cfg.signal_sl_min_points)
            tp = leg_entry + tp_distance if side == "buy" else leg_entry - tp_distance
            loss_per_lot = calc_loss_per_lot(symbol_name, side, leg_entry, sl)
            leg_risk_usd = risk_usd
            if order_kind == "market":
                leg_risk_usd *= max(
                    0.0,
                    _env_float("PHOENIX_RANGE_MARKET_RISK_MULTIPLIER", 0.5),
                )
            requested_volume = leg_risk_usd / loss_per_lot if loss_per_lot > 0 else cfg.min_lot
            minimum_leg_lot = max(cfg.min_lot, _env_float("PHOENIX_MIN_LEG_LOT", cfg.min_lot))
            selected_volume = (
                max(minimum_leg_lot, requested_volume)
                if range_per_leg_risk_pct > 0.0
                else fixed_volume if fixed_volume > 0 else max(minimum_leg_lot, requested_volume)
            )
            volume = normalize_volume(
                symbol_name,
                selected_volume,
                cfg.min_lot,
                (
                    max(cfg.max_lot, cfg.signal_dynamic_lot_max)
                    if range_per_leg_risk_pct > 0.0
                    else max(max_volume, fixed_volume)
                ),
            )
            if order_kind == "market":
                market_max_lot = max(
                    cfg.min_lot,
                    _env_float("PHOENIX_RANGE_MARKET_MAX_LOT", max_volume),
                )
                volume = normalize_volume(
                    symbol_name,
                    min(volume, market_max_lot),
                    cfg.min_lot,
                    market_max_lot,
                )
                log.info(
                    f"[RISK LOT] PHOENIX range MARKET multiplier="
                    f"{_env_float('PHOENIX_RANGE_MARKET_RISK_MULTIPLIER', 0.5):.2f} "
                    f"risk_usd={leg_risk_usd:.2f} volume={volume:.2f}"
                )
            comment = f"PHOENIX:RANGE {'M' if order_kind == 'market' else 'P'}{leg_index}"
            if order_kind == "limit":
                result = send_pending_order(
                    symbol=symbol_name,
                    side=side,
                    order_kind="limit",
                    volume=volume,
                    price=leg_entry,
                    sl=sl,
                    tp=tp,
                    deviation=cfg.deviation,
                    magic=cfg.magic,
                    comment=comment,
                )
            else:
                result = send_market_order(
                    symbol=symbol_name,
                    side=side,
                    volume=volume,
                    sl=sl,
                    tp=tp,
                    deviation=cfg.deviation,
                    magic=cfg.magic,
                    comment=comment,
                )
            retcode = getattr(result, "retcode", None)
            success = retcode in {10008, 10009}
            successful_legs += int(success)
            order_ticket = int(getattr(result, "order", 0) or 0)
            if success and order_kind == "limit" and order_ticket:
                pending_records.append(
                    {
                        "order_ticket": order_ticket,
                        "chat_id": int(chat_id or 0),
                        "message_id": int(message_id or 0),
                        "side": side,
                        "created_utc": created_utc.isoformat(),
                        "expires_utc": (created_utc + timedelta(minutes=expiry_minutes)).isoformat(),
                    }
                )
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_range_trigger_attempt",
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "message_id": message_id,
                    "leg": leg_index,
                    "order_kind": order_kind,
                    "market_chase": bool(market_is_chase and order_kind == "market"),
                    "side": side,
                    "range": [low, high],
                    "entry": leg_entry,
                    "sl": sl,
                    "tp": tp,
                    "risk_pct_total": risk_pct,
                    "risk_pct_per_leg": range_per_leg_risk_pct,
                    "volume": volume,
                    "retcode": retcode,
                },
            )
            if success:
                log.info(
                    f"[PHOENIX-RANGE] opened leg={leg_index}/{len(planned_legs)} {side.upper()} "
                    f"kind={order_kind} entry={leg_entry:.2f} sl={sl:.2f} tp={tp:.2f} volume={volume:.2f}"
                )
            else:
                log.warning(f"[PHOENIX-RANGE] leg={leg_index} rejected retcode={retcode}")

        if market_is_usable and _env_bool("PHOENIX_RANGE_OPTIMIZED_EXTRA_ENABLED", False):
            optimized_targets = []
            for raw_target in str(
                os.getenv("PHOENIX_RANGE_OPTIMIZED_EXTRA_TPS_USD", "2,2,2.5") or "2,2,2.5"
            ).split(","):
                try:
                    optimized_targets.append(max(0.1, float(raw_target.strip())))
                except ValueError:
                    continue
            optimized_targets = optimized_targets or [2.0, 2.0, 2.5]
            optimized_sl_distance = max(
                max(optimized_targets),
                _env_float("PHOENIX_RANGE_OPTIMIZED_EXTRA_SL_USD", 8.0),
            )
            optimized_risk_usd = _risk_usd_per_leg(
                balance,
                risk_pct,
                len(optimized_targets),
                range_per_leg_risk_pct,
            )
            optimized_minimum_leg_lot = max(
                cfg.min_lot,
                _env_float("PHOENIX_MIN_LEG_LOT", cfg.min_lot),
            )
            for optimized_index, tp_distance in enumerate(optimized_targets, start=1):
                optimized_sl = entry - optimized_sl_distance if side == "buy" else entry + optimized_sl_distance
                optimized_sl = _minimum_safe_stop(
                    symbol_name,
                    side,
                    entry,
                    optimized_sl,
                    spread_points,
                    cfg.signal_sl_min_points,
                )
                optimized_tp = entry + tp_distance if side == "buy" else entry - tp_distance
                optimized_loss_per_lot = calc_loss_per_lot(symbol_name, side, entry, optimized_sl)
                optimized_requested_volume = (
                    optimized_risk_usd / optimized_loss_per_lot
                    if optimized_loss_per_lot > 0
                    else cfg.min_lot
                )
                optimized_volume = normalize_volume(
                    symbol_name,
                    (
                        max(optimized_minimum_leg_lot, optimized_requested_volume)
                        if range_per_leg_risk_pct > 0.0
                        else fixed_volume
                        if fixed_volume > 0
                        else max(optimized_minimum_leg_lot, optimized_requested_volume)
                    ),
                    cfg.min_lot,
                    (
                        max(cfg.max_lot, cfg.signal_dynamic_lot_max)
                        if range_per_leg_risk_pct > 0.0
                        else max(max_volume, fixed_volume)
                    ),
                )
                optimized_result = send_market_order(
                    symbol=symbol_name,
                    side=side,
                    volume=optimized_volume,
                    sl=optimized_sl,
                    tp=optimized_tp,
                    deviation=cfg.deviation,
                    magic=cfg.magic,
                    comment=f"PHOENIX:RANGE OPT{optimized_index}",
                )
                optimized_retcode = getattr(optimized_result, "retcode", None)
                optimized_success = optimized_retcode in {10008, 10009}
                successful_legs += int(optimized_success)
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_range_optimized_extra_attempt",
                        "chat_id": int(chat_id or 0),
                        "message_id": int(message_id or 0),
                        "side": side,
                        "leg_index": optimized_index,
                        "entry": entry,
                        "sl": optimized_sl,
                        "tp": optimized_tp,
                        "tp_distance": tp_distance,
                        "volume": optimized_volume,
                        "retcode": optimized_retcode,
                    },
                )
                if optimized_success:
                    log.info(
                        f"[PHOENIX-RANGE-OPT] opened leg={optimized_index}/{len(optimized_targets)} "
                        f"{side.upper()} entry={entry:.2f} sl={optimized_sl:.2f} "
                        f"tp={optimized_tp:.2f} volume={optimized_volume:.2f}"
                    )
                else:
                    log.warning(
                        f"[PHOENIX-RANGE-OPT] leg={optimized_index} rejected retcode={optimized_retcode}"
                    )
        if successful_legs:
            direction_hint = state.get("phoenix_direction_hints", {}).get(
                str(int(chat_id or 0)), {}
            )
            direction_message_id = (
                int(direction_hint.get("message_id", 0) or 0)
                if isinstance(direction_hint, dict)
                else 0
            )
            opened_messages.append(key)
            state["phoenix_range_trigger_messages"] = opened_messages[-250:]
            state["phoenix_range_pending_orders"] = pending_records[-500:]
            state.setdefault("phoenix_recent_ranges", {})[str(int(chat_id or 0))] = {
                "side": side,
                "low": low,
                "high": high,
                "message_id": int(message_id or 0),
                "direction_message_id": direction_message_id,
                "cycle_id": f"{int(chat_id or 0)}:{direction_message_id or int(message_id or 0)}",
                "created_utc": created_utc.isoformat(),
                "successful_legs": successful_legs,
            }
            _save_runtime_state()
            log.info(
                f"[PHOENIX-RANGE] sequence complete: opened={successful_legs}/"
                f"{len(planned_legs) + (len(optimized_targets) if market_is_usable and _env_bool('PHOENIX_RANGE_OPTIMIZED_EXTRA_ENABLED', False) else 0)} "
                f"side={side.upper()} range={low:.2f}-{high:.2f} "
                f"risk_per_leg={range_per_leg_risk_pct:.2f}% fallback_total_risk={risk_pct:.2f}%"
            )
        return successful_legs > 0

    def _validate_phoenix_direction_runner_with_range(
        chat_id: int | None,
        side: str,
        entry_range: list[float],
    ) -> bool:
        symbol_name = symbol_by_asset["gold"]
        tolerance = max(0.0, _env_float("PHOENIX_RANGE_TRIGGER_TOLERANCE_USD", 2.0))
        records = state.get("phoenix_direction_runner_positions", {})
        if not isinstance(records, dict):
            return False
        adopted = False
        changed = False
        for position in positions_by_magic(symbol_name, cfg.magic):
            comment = str(getattr(position, "comment", "") or "")
            if not comment.startswith("PHOENIX:DIR RUNN"):
                continue
            ticket = int(getattr(position, "ticket", 0) or 0)
            record = records.get(str(ticket), {})
            if int(record.get("chat_id", 0) or 0) != int(chat_id or 0):
                continue
            position_side = (
                "buy"
                if int(getattr(position, "type", -1)) == int(mt5.POSITION_TYPE_BUY)
                else "sell"
            )
            runner_entry = float(getattr(position, "price_open", 0.0) or 0.0)
            matches = position_side == side and _phoenix_runner_matches_range(
                side,
                runner_entry,
                entry_range,
                tolerance,
            )
            if matches:
                record["range_confirmed"] = True
                record["range"] = [min(entry_range), max(entry_range)]
                records[str(ticket)] = record
                adopted = True
                changed = True
                log.info(
                    f"[PHOENIX DIR RUNNER] confirmed by range ticket={ticket} "
                    f"entry={runner_entry:.2f} range={min(entry_range):.2f}-{max(entry_range):.2f}"
                )
                continue
            result = close_position(position, deviation=cfg.deviation)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_direction_runner_range_validation",
                    "position_ticket": ticket,
                    "side": position_side,
                    "announced_side": side,
                    "entry": runner_entry,
                    "range": [min(entry_range), max(entry_range)],
                    "tolerance_usd": tolerance,
                    "action": "close_misaligned",
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                records.pop(str(ticket), None)
                changed = True
                log.info(
                    f"[PHOENIX DIR RUNNER] closed after range mismatch ticket={ticket} "
                    f"entry={runner_entry:.2f} range={min(entry_range):.2f}-{max(entry_range):.2f}"
                )
            else:
                log.warning(
                    f"[PHOENIX DIR RUNNER] range mismatch close rejected ticket={ticket} retcode={retcode}"
                )
        if changed:
            state["phoenix_direction_runner_positions"] = records
            _save_runtime_state()
        return adopted

    def _phoenix_preliminary_range_covers_signal(signal: ParsedSignal) -> dict | None:
        records = state.get("phoenix_recent_ranges", {})
        record = records.get(str(int(signal.chat_id or 0)), {}) if isinstance(records, dict) else {}
        if not isinstance(record, dict) or str(record.get("side", "")).lower() != signal.side:
            return None
        try:
            created = datetime.fromisoformat(str(record.get("created_utc", "")).replace("Z", "+00:00"))
        except Exception:
            return None
        max_age = max(30.0, _env_float("PHOENIX_PRELIMINARY_RANGE_MATCH_SECONDS", 300.0))
        if (datetime.now(UTC) - created).total_seconds() > max_age:
            return None
        entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
        if len(entries) < 2:
            return None
        tolerance = max(0.25, _env_float("PHOENIX_PRELIMINARY_RANGE_MATCH_TOLERANCE_USD", 2.0))
        if abs(min(entries) - float(record.get("low", 0.0))) > tolerance:
            return None
        if abs(max(entries) - float(record.get("high", 0.0))) > tolerance:
            return None
        return record

    def _active_symbols() -> set[str]:
        return {str(symbol) for symbol in symbol_by_asset.values() if symbol}

    def _symbol_for_signal(signal: ParsedSignal) -> str | None:
        if signal.asset in symbol_by_asset:
            return symbol_by_asset.get(signal.asset)
        resolved = _optional_asset_symbol(signal.asset)
        symbol_by_asset[signal.asset] = resolved
        return resolved

    def _prune_recent_signatures(now: datetime | None = None) -> dict:
        channel_window = max(1.0, _env_float("SIGNAL_DUPLICATE_WINDOW_MINUTES", 30.0))
        global_window = max(0.0, _env_float("SIGNAL_GLOBAL_DUPLICATE_WINDOW_MINUTES", 3.0))
        ghp_window = max(0.0, _env_float("GHP_CROSS_CHANNEL_DUPLICATE_WINDOW_MINUTES", 20.0))
        relay_window = max(0.0, _env_float("DANY_RELAY_DUPLICATE_WINDOW_MINUTES", 10080.0))
        cutoff = (now or datetime.now(UTC)) - timedelta(
            minutes=max(channel_window, global_window, ghp_window, relay_window, 5.0)
        )
        recent = state.get("recent_signal_signatures", {})
        if not isinstance(recent, dict):
            recent = {}
        kept = {}
        for signature, seen_at in recent.items():
            try:
                seen_dt = datetime.fromisoformat(str(seen_at).replace("Z", "+00:00"))
            except Exception:
                continue
            if seen_dt >= cutoff:
                kept[str(signature)] = seen_dt.isoformat()
        state["recent_signal_signatures"] = kept
        return kept

    def _is_recent_duplicate(signal: ParsedSignal) -> bool:
        signature = _signal_content_signature(signal)
        global_signature = _signal_global_content_signature(signal)
        ghp_signature = _ghp_family_content_signature(signal)
        ghp_trade_signature = _ghp_family_trade_signature(signal)
        relay_signature = _relay_content_signature(signal)
        allow_dany_source_duplicates = _env_bool("DANY_ALLOW_SOURCE_DUPLICATES", False)
        dany_source = _is_dany_signals_source(signal.chat_id, signal.chat_title)
        recent = _prune_recent_signatures()
        now = datetime.now(UTC)

        def seen_within(key: str, minutes: float) -> bool:
            if minutes <= 0 or key not in recent:
                return False
            try:
                seen_at = datetime.fromisoformat(str(recent[key]).replace("Z", "+00:00"))
            except Exception:
                return False
            return now - seen_at <= timedelta(minutes=float(minutes))

        relay_window = _env_float("DANY_RELAY_DUPLICATE_WINDOW_MINUTES", 10080.0)
        opposite_relay_key = (
            f"source:{relay_signature}"
            if dany_source
            else f"dany:{relay_signature}"
        )
        if seen_within(signature, _env_float("SIGNAL_DUPLICATE_WINDOW_MINUTES", 30.0)):
            return True
        if allow_dany_source_duplicates and dany_source:
            return False
        return seen_within(
            global_signature,
            _env_float("SIGNAL_GLOBAL_DUPLICATE_WINDOW_MINUTES", 3.0),
        ) or (
            not allow_dany_source_duplicates
            and seen_within(opposite_relay_key, relay_window)
        ) or bool(
            ghp_signature
            and seen_within(
                ghp_signature,
                _env_float("GHP_CROSS_CHANNEL_DUPLICATE_WINDOW_MINUTES", 20.0),
            )
        ) or bool(
            ghp_trade_signature
            and seen_within(
                ghp_trade_signature,
                _env_float("GHP_CROSS_CHANNEL_DUPLICATE_WINDOW_MINUTES", 20.0),
            )
        )

    def _remember_signal_signature(signal: ParsedSignal) -> None:
        recent = _prune_recent_signatures()
        now = datetime.now(UTC).isoformat()
        recent[_signal_content_signature(signal)] = now
        dany_independent = _env_bool("DANY_ALLOW_SOURCE_DUPLICATES", False) and _is_dany_signals_source(
            signal.chat_id,
            signal.chat_title,
        )
        if not dany_independent:
            recent[_signal_global_content_signature(signal)] = now
            ghp_signature = _ghp_family_content_signature(signal)
            if ghp_signature:
                recent[ghp_signature] = now
            ghp_trade_signature = _ghp_family_trade_signature(signal)
            if ghp_trade_signature:
                recent[ghp_trade_signature] = now
        relay_scope = "dany" if _is_dany_signals_source(signal.chat_id, signal.chat_title) else "source"
        recent[f"{relay_scope}:{_relay_content_signature(signal)}"] = now
        state["recent_signal_signatures"] = recent

    def _seed_recent_signatures_from_managed() -> None:
        recent = _prune_recent_signatures()
        cutoff = datetime.now(UTC) - timedelta(hours=6)
        for signal_snapshot in (state.get("last_signal"), (state.get("last_order") or {}).get("signal") if isinstance(state.get("last_order"), dict) else None):
            if not isinstance(signal_snapshot, dict):
                continue
            try:
                signature = _signal_content_signature_values(
                    str(signal_snapshot.get("asset", "gold") or "gold"),
                    int(signal_snapshot.get("chat_id", 0) or 0),
                    str(signal_snapshot.get("side", "")),
                    str(signal_snapshot.get("order_kind", "market") or "market"),
                    [float(value) for value in signal_snapshot.get("entries", [])],
                    float(signal_snapshot.get("sl", 0.0) or 0.0),
                    [float(value) for value in signal_snapshot.get("tps", [])],
                )
                global_signature = _signal_global_content_signature_values(
                    str(signal_snapshot.get("asset", "gold") or "gold"),
                    str(signal_snapshot.get("side", "")),
                    str(signal_snapshot.get("order_kind", "market") or "market"),
                    [float(value) for value in signal_snapshot.get("entries", [])],
                    float(signal_snapshot.get("sl", 0.0) or 0.0),
                    [float(value) for value in signal_snapshot.get("tps", [])],
                )
            except Exception:
                continue
            now = datetime.now(UTC).isoformat()
            recent[signature] = now
            recent[global_signature] = now
        for managed in managed_state.get("signals", {}).values():
            try:
                created = datetime.fromisoformat(str(managed.get("created_utc", "")).replace("Z", "+00:00"))
            except Exception:
                continue
            if created < cutoff:
                continue
            try:
                signature = _signal_content_signature_values(
                    str(managed.get("asset", "gold") or "gold"),
                    int(str(managed.get("signal_uid", "0:")).split(":", 1)[0]),
                    str(managed.get("side", "")),
                    str(managed.get("order_kind", "market") or "market"),
                    [float(managed.get("entry", 0.0) or 0.0)],
                    float(managed.get("initial_sl", 0.0) or 0.0),
                    [float(value) for value in managed.get("tps", [])],
                )
                global_signature = _signal_global_content_signature_values(
                    str(managed.get("asset", "gold") or "gold"),
                    str(managed.get("side", "")),
                    str(managed.get("order_kind", "market") or "market"),
                    [float(managed.get("entry", 0.0) or 0.0)],
                    float(managed.get("initial_sl", 0.0) or 0.0),
                    [float(value) for value in managed.get("tps", [])],
                )
            except Exception:
                continue
            recent[signature] = created.isoformat()
            recent[global_signature] = created.isoformat()
        state["recent_signal_signatures"] = recent
        _save_runtime_state()

    _seed_recent_signatures_from_managed()

    def _session_net_result() -> tuple[float, float, float, float]:
        current_account = account_info()
        current_balance = float(getattr(current_account, "balance", 0.0) or 0.0)
        current_equity = float(getattr(current_account, "equity", 0.0) or current_balance)
        return (
            current_balance,
            current_equity,
            current_balance - session_start_balance,
            current_equity - session_start_equity,
        )

    session_stop_state = {"triggered": False}

    def _emergency_flatten_all(reason: str, current_balance: float, current_equity: float) -> None:
        for managed_symbol in _active_symbols():
            for order in orders_by_magic(managed_symbol, cfg.magic):
                ticket = int(getattr(order, "ticket", 0) or 0)
                result = remove_order(order, magic=cfg.magic)
                _append_jsonl(
                    events_path,
                    {
                        "type": "emergency_pending_remove_attempt",
                        "reason": reason,
                        "symbol": managed_symbol,
                        "order_ticket": ticket,
                        "retcode": getattr(result, "retcode", None),
                        "current_balance": current_balance,
                        "current_equity": current_equity,
                    },
                )
            for position in positions_by_magic(managed_symbol, cfg.magic):
                ticket = int(getattr(position, "ticket", 0) or 0)
                result = close_position(position, deviation=cfg.deviation)
                _append_jsonl(
                    events_path,
                    {
                        "type": "emergency_position_close_attempt",
                        "reason": reason,
                        "symbol": managed_symbol,
                        "position_ticket": ticket,
                        "volume": float(getattr(position, "volume", 0.0) or 0.0),
                        "profit": float(getattr(position, "profit", 0.0) or 0.0),
                        "retcode": getattr(result, "retcode", None),
                        "current_balance": current_balance,
                        "current_equity": current_equity,
                    },
                )

    async def _stop_if_session_limit_reached() -> bool:
        if session_stop_state["triggered"]:
            return False
        if cfg.signal_session_net_profit_stop_usd <= 0 and cfg.signal_session_net_loss_stop_usd <= 0:
            return False
        current_balance, current_equity, balance_net, equity_net = _session_net_result()
        stop_reason = ""
        target_usd = 0.0
        if cfg.signal_session_net_profit_stop_usd > 0 and balance_net >= cfg.signal_session_net_profit_stop_usd:
            stop_reason = "profit_target"
            target_usd = cfg.signal_session_net_profit_stop_usd
        if cfg.signal_session_net_loss_stop_usd > 0 and equity_net <= -cfg.signal_session_net_loss_stop_usd:
            stop_reason = "loss_limit"
            target_usd = cfg.signal_session_net_loss_stop_usd
        if not stop_reason:
            return False
        session_stop_state["triggered"] = True
        _append_jsonl(
            events_path,
            {
                "type": "session_limit_reached",
                "reason": stop_reason,
                "session_start_balance": session_start_balance,
                "session_start_equity": session_start_equity,
                "current_balance": current_balance,
                "current_equity": current_equity,
                "session_balance_net": balance_net,
                "session_equity_net": equity_net,
                "target_usd": target_usd,
            },
        )
        if stop_reason == "loss_limit":
            log.info(f"[STOP] session net loss limit reached: {equity_net:.2f} <= -{target_usd:.2f} USD")
            _emergency_flatten_all(stop_reason, current_balance, current_equity)
        else:
            log.info(f"[STOP] session net profit target reached: {balance_net:.2f} >= {target_usd:.2f} USD")
        await client.disconnect()
        return True

    def _remember_managed_signal(signal: ParsedSignal, payload: dict, entry_price: float, sl: float, execution_tp: float, strategy: ChannelStrategy) -> None:
        signal_id = _managed_signal_id(signal)
        managed = {
            "signal_id": signal_id,
            "signal_uid": signal.uid,
            "chat_id": int(signal.chat_id or 0),
            "chat_title": signal.chat_title,
            "asset": signal.asset,
            "symbol": str(payload.get("symbol", "") or _symbol_for_signal(signal)),
            "strategy": strategy.name,
            "protect_mode": strategy.protect_mode,
            "atr_mult": float(strategy.atr_mult),
            "strategy_pending_expiry_minutes": float(strategy.pending_expiry_minutes or 0.0),
            "side": signal.side,
            "order_kind": signal.order_kind,
            "provider_explicit_pending": _is_explicit_provider_pending(signal),
            "entry_mode": str(payload.get("entry_mode", "")),
            "entry_index": int(payload.get("entry_index", 0) or 0),
            "target_plan_index": int(payload.get("target_plan_index", 0) or 0),
            "pending_cancel_tp_index": (
                max(1, int(_env_float("PHOENIX_PENDING_CANCEL_TP_LEVEL", 2.0)))
                if _is_phoenix_source(signal.chat_id, signal.chat_title)
                else max(1, int(_env_float("SIGNAL_PENDING_CANCEL_TP_LEVEL", 1.0)))
            ),
            "pending_expiry_override_minutes": float(payload.get("pending_expiry_override_minutes", 0.0) or 0.0),
            "ignore_channel_pending_cancel": bool(payload.get("ignore_channel_pending_cancel", False)),
            "execution_module": str(payload.get("execution_module", "") or ""),
            "entries_count": int(payload.get("entries_count", 0) or len(signal.entries)),
            "entry": float(entry_price),
            "initial_sl": float(sl),
            "tp1": float(payload.get("tp1", signal.tps[0])),
            "execution_tp": float(execution_tp),
            "tps": [float(value) for value in signal.tps],
            "order_ticket": int(payload.get("order_ticket", 0) or 0),
            "deal_ticket": int(payload.get("deal_ticket", 0) or 0),
            "protected_to_tp1": False,
            "created_utc": datetime.now(UTC).isoformat(),
            "min_hold_seconds": _min_hold_seconds(),
        }
        if _is_phoenix_source(signal.chat_id, signal.chat_title):
            preliminary = _phoenix_preliminary_range_covers_signal(signal)
            if preliminary:
                aliases = {
                    int(signal.message_id or 0),
                    int(preliminary.get("message_id", 0) or 0),
                    int(preliminary.get("direction_message_id", 0) or 0),
                }
                managed["provider_revision_message_ids"] = sorted(
                    message_id for message_id in aliases if message_id > 0
                )
                managed["phoenix_cycle_id"] = str(preliminary.get("cycle_id", "") or "")
        managed_state.setdefault("signals", {})[signal_id] = managed
        _save_managed_state(managed_path, managed_state)

    def _managed_message_key(managed: dict) -> str:
        uid = str(managed.get("signal_uid", "") or "")
        parts = uid.split(":", 2)
        if len(parts) >= 2:
            return f"{parts[0]}:{parts[1]}"
        return f"{managed.get('chat_id', 0)}:{managed.get('signal_id', '')}"

    def _active_managed_message_keys_for_channel(chat_id: int | None, chat_title: str) -> set[str]:
        live_tickets: set[int] = set()
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                live_tickets.add(int(getattr(position, "ticket", 0) or 0))
            for order in orders_by_magic(managed_symbol, cfg.magic):
                live_tickets.add(int(getattr(order, "ticket", 0) or 0))

        normalized_title = str(chat_title or "").strip().lower()
        keys: set[str] = set()
        for managed in managed_state.setdefault("signals", {}).values():
            order_ticket = int(managed.get("order_ticket", 0) or 0)
            deal_ticket = int(managed.get("deal_ticket", 0) or 0)
            if order_ticket not in live_tickets and deal_ticket not in live_tickets:
                continue
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if same_chat or same_title:
                keys.add(_managed_message_key(managed))
        return keys

    def _recent_managed_message_keys_for_channel(chat_id: int | None, chat_title: str, minutes: float) -> set[str]:
        cutoff = datetime.now(UTC) - timedelta(minutes=float(minutes))
        normalized_title = str(chat_title or "").strip().lower()
        keys: set[str] = set()
        for managed in managed_state.setdefault("signals", {}).values():
            try:
                created = datetime.fromisoformat(str(managed.get("created_utc", "")).replace("Z", "+00:00"))
            except Exception:
                continue
            if created < cutoff:
                continue
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if same_chat or same_title:
                keys.add(_managed_message_key(managed))
        return keys

    def _managed_learning_key(managed: dict) -> str:
        chat_id = int(managed.get("chat_id", 0) or 0)
        title = str(managed.get("chat_title", "") or "").strip().lower()
        explicit = {
            -1002864291293: "phoenixvip",
            -1001704634655: "goldhunterfx",
            -1001914224843: "xaugoldsign",
        }
        if chat_id in explicit:
            return explicit[chat_id]
        if "royal" in title:
            return "royalgold"
        if "phoenix" in title:
            return "phoenixvip"
        if "goldhunter" in title or "gold hunter" in title:
            return "goldhunterfx"
        if "xauusd gold signal" in title:
            return "xaugoldsign"
        return str(chat_id or title or "unknown")

    def _signal_learning_key(signal: ParsedSignal) -> str:
        return _managed_learning_key({"chat_id": signal.chat_id or 0, "chat_title": signal.chat_title})

    def _adaptive_state() -> dict:
        adaptive = state.get("adaptive_learning")
        if not isinstance(adaptive, dict):
            adaptive = {}
        adaptive.setdefault("closed_positions", {})
        adaptive.setdefault("channels", {})
        adaptive.setdefault("last_scan_epoch", 0)
        state["adaptive_learning"] = adaptive
        return adaptive

    def _adaptive_channel_paused(signal: ParsedSignal) -> tuple[bool, str]:
        if not cfg.signal_adaptive_learning_enabled or not _env_bool("SIGNAL_ADAPTIVE_ALLOW_CHANNEL_PAUSE", False):
            return False, _signal_learning_key(signal)
        adaptive = _adaptive_state()
        key = _signal_learning_key(signal)
        channel = adaptive.get("channels", {}).get(key, {})
        paused_until = str(channel.get("paused_until_utc", "") or "")
        if not paused_until:
            return False, key
        try:
            paused_dt = datetime.fromisoformat(paused_until.replace("Z", "+00:00"))
        except Exception:
            return False, key
        return datetime.now(UTC) < paused_dt, key

    def _scan_closed_trades_for_learning(now: datetime) -> bool:
        if not cfg.signal_adaptive_learning_enabled:
            return False
        adaptive = _adaptive_state()
        last_scan = float(adaptive.get("last_scan_epoch", 0) or 0)
        if time.time() - last_scan < 60.0:
            return False

        adaptive["last_scan_epoch"] = time.time()
        closed_positions = adaptive.setdefault("closed_positions", {})
        channels = adaptive.setdefault("channels", {})
        ticket_to_managed: dict[int, dict] = {}
        for managed in managed_state.setdefault("signals", {}).values():
            for key in ("order_ticket", "deal_ticket"):
                try:
                    ticket = int(managed.get(key, 0) or 0)
                except Exception:
                    ticket = 0
                if ticket:
                    ticket_to_managed[ticket] = managed

        start = now - timedelta(days=7)
        try:
            deals = mt5.history_deals_get(start, now + timedelta(minutes=1)) or []
        except Exception as exc:
            log.warning(f"[LEARN] history scan failed: {type(exc).__name__}: {exc}")
            return True

        changed = True
        for deal in deals:
            try:
                entry_type = int(getattr(deal, "entry", 0) or 0)
                position_id = int(getattr(deal, "position_id", 0) or 0)
            except Exception:
                continue
            if entry_type != 1 or position_id <= 0 or str(position_id) in closed_positions:
                continue
            managed = ticket_to_managed.get(position_id)
            if not managed:
                continue
            key = _managed_learning_key(managed)
            profit = float(getattr(deal, "profit", 0.0) or 0.0)
            if profit > 0.5:
                outcome = "win"
            elif profit < -0.5:
                outcome = "loss"
            else:
                outcome = "be"
            closed_positions[str(position_id)] = {
                "channel": key,
                "profit": round(profit, 2),
                "outcome": outcome,
                "closed_utc": datetime.fromtimestamp(int(getattr(deal, "time", time.time()) or time.time()), UTC).isoformat(),
            }
            channel = channels.setdefault(key, {"recent": []})
            recent = channel.setdefault("recent", [])
            recent.append({"outcome": outcome, "profit": round(profit, 2), "utc": closed_positions[str(position_id)]["closed_utc"]})
            channel["recent"] = recent[-30:]

        for key, channel in channels.items():
            recent = list(channel.get("recent", []))[-12:]
            trades = len(recent)
            wins = sum(1 for item in recent if item.get("outcome") == "win")
            losses = sum(1 for item in recent if item.get("outcome") == "loss")
            bes = sum(1 for item in recent if item.get("outcome") == "be")
            channel["rolling_trades"] = trades
            channel["rolling_wins"] = wins
            channel["rolling_losses"] = losses
            channel["rolling_be"] = bes
            channel["rolling_non_loss_rate"] = round(((wins + bes) / max(1, trades)) * 100.0, 2)
            channel["rolling_win_ex_be"] = round((wins / max(1, wins + losses)) * 100.0, 2)
            currently_paused = False
            try:
                currently_paused = datetime.now(UTC) < datetime.fromisoformat(str(channel.get("paused_until_utc", "")).replace("Z", "+00:00"))
            except Exception:
                currently_paused = False
            if (
                _env_bool("SIGNAL_ADAPTIVE_ALLOW_CHANNEL_PAUSE", False)
                and trades >= 8
                and losses >= 4
                and channel["rolling_non_loss_rate"] < 70.0
                and not currently_paused
            ):
                if key == "unknown" or key.startswith("0:"):
                    continue
                channel["paused_until_utc"] = (now + timedelta(hours=24)).isoformat()
                channel["pause_reason"] = "rolling_non_loss_below_70"
                log.info(
                    f"[LEARN] paused channel {key} for 24h: trades={trades} "
                    f"wins={wins} losses={losses} be={bes} non_loss={channel['rolling_non_loss_rate']:.1f}%"
                )

        _append_jsonl(
            events_path,
            {
                "type": "learning_scan",
                "channels": {
                    key: {
                        "rolling_trades": value.get("rolling_trades", 0),
                        "rolling_wins": value.get("rolling_wins", 0),
                        "rolling_losses": value.get("rolling_losses", 0),
                        "rolling_be": value.get("rolling_be", 0),
                        "rolling_non_loss_rate": value.get("rolling_non_loss_rate", 0),
                        "paused_until_utc": value.get("paused_until_utc", ""),
                    }
                    for key, value in channels.items()
                },
            },
        )
        _save_runtime_state()
        return changed

    def _pending_cancel_tp_index(managed: dict) -> int:
        try:
            configured = managed.get("pending_cancel_tp_index")
            if configured is not None:
                return max(1, int(configured or 1))
            if _is_phoenix_source(managed.get("chat_id"), str(managed.get("chat_title", "") or "")):
                return max(1, int(_env_float("PHOENIX_PENDING_CANCEL_TP_LEVEL", 2.0)))
            return max(1, int(_env_float("SIGNAL_PENDING_CANCEL_TP_LEVEL", 1.0)))
        except (TypeError, ValueError):
            return 1

    def _pending_cancel_level_reached(managed: dict, current_price: float) -> bool:
        side = str(managed.get("side", "") or "")
        tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
        cancel_tp_index = _pending_cancel_tp_index(managed)
        if side not in {"buy", "sell"} or len(tps) < cancel_tp_index:
            return False
        is_phoenix = _is_phoenix_source(managed.get("chat_id"), str(managed.get("chat_title", "") or ""))
        tolerance = _env_float(
            "PHOENIX_PENDING_CANCEL_TP_TOLERANCE_USD" if is_phoenix else "SIGNAL_PENDING_CANCEL_TP_TOLERANCE_USD",
            0.25 if is_phoenix else 0.0,
        )
        target = tps[cancel_tp_index - 1]
        if side == "buy":
            return float(current_price) >= float(target) - max(0.0, tolerance)
        return float(current_price) <= float(target) + max(0.0, tolerance)

    def _protect_groups_after_tp1(signals: dict, now: datetime) -> bool:
        positions = []
        orders = []
        for managed_symbol in _active_symbols():
            positions.extend(positions_by_magic(managed_symbol, cfg.magic))
            orders.extend(orders_by_magic(managed_symbol, cfg.magic))

        position_matches: list[tuple[object, str, dict]] = []
        for position in positions:
            for signal_id, managed in list(signals.items()):
                if _position_matches_managed(position, managed):
                    position_matches.append((position, signal_id, managed))
                    break

        groups: dict[str, list[tuple[object, str, dict]]] = {}
        for item in position_matches:
            groups.setdefault(_managed_message_key(item[2]), []).append(item)

        changed = False
        for group_key, items in groups.items():
            group_reached_tp1 = False
            for position, _signal_id, managed in items:
                if _hold_remaining_seconds(position, managed) > 0:
                    continue
                side = str(managed.get("side", "") or "")
                tp1 = float(managed.get("tp1", 0.0) or 0.0)
                if side not in {"buy", "sell"} or tp1 <= 0:
                    continue
                tick = get_tick(str(getattr(position, "symbol", "") or managed.get("symbol", symbol_by_asset["gold"])))
                current_price = float(tick.bid if side == "buy" else tick.ask)
                if _price_reached_tp(side, current_price, tp1):
                    group_reached_tp1 = True
                    break
            if not group_reached_tp1:
                continue

            for order in list(orders):
                ticket = int(getattr(order, "ticket", 0) or 0)
                managed_match = None
                for signal_id, managed in list(signals.items()):
                    if _managed_message_key(managed) != group_key:
                        continue
                    try:
                        if int(managed.get("order_ticket", 0) or 0) == ticket:
                            managed_match = (signal_id, managed)
                            break
                    except Exception:
                        continue
                if managed_match is None:
                    continue
                signal_id, managed = managed_match
                if bool(managed.get("ignore_channel_pending_cancel", False)):
                    continue
                side = str(managed.get("side", "") or "")
                tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
                cancel_tp_index = _pending_cancel_tp_index(managed)
                if side not in {"buy", "sell"} or len(tps) < cancel_tp_index:
                    continue
                order_symbol = str(getattr(order, "symbol", "") or managed.get("symbol", symbol_by_asset["gold"]))
                order_tick = get_tick(order_symbol)
                order_price = float(order_tick.bid if side == "buy" else order_tick.ask)
                if not _pending_cancel_level_reached(managed, order_price):
                    continue
                result = remove_order(order, magic=cfg.magic)
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "group_tp_pending_cancel_attempt",
                        "signal_id": signal_id,
                        "group_key": group_key,
                        "order_ticket": ticket,
                        "cancel_tp_index": cancel_tp_index,
                        "retcode": retcode,
                    },
                )
                if retcode in {10008, 10009}:
                    managed["group_tp_cancelled"] = True
                    managed["group_tp_cancelled_utc"] = now.isoformat()
                    changed = True
                    log.info(
                        f"[PROTECT] group TP{cancel_tp_index} cancelled pending {ticket} "
                        f"for {signal_id}: retcode={retcode}"
                    )

            for position, signal_id, managed in items:
                if _hold_remaining_seconds(position, managed) > 0:
                    continue
                protect_mode = str(managed.get("protect_mode", "be") or "be").lower()
                if protect_mode in {"none", "be_after_tp2", "be_after_tp3", "tp1_after_tp3"}:
                    continue
                side = str(managed.get("side", "") or "")
                entry = float(managed.get("entry", 0.0) or getattr(position, "price_open", 0.0) or 0.0)
                execution_tp = float(managed.get("execution_tp", 0.0) or getattr(position, "tp", 0.0) or 0.0)
                if side not in {"buy", "sell"} or entry <= 0 or execution_tp <= 0:
                    continue
                current_sl = float(getattr(position, "sl", 0.0) or 0.0)
                new_sl = _better_stop(side, current_sl, entry)
                if current_sl > 0 and abs(new_sl - current_sl) < 0.01:
                    continue
                position_symbol = str(
                    getattr(position, "symbol", "") or managed.get("symbol", symbol_by_asset["gold"])
                )
                tick = get_tick(position_symbol)
                current_price = float(tick.bid if side == "buy" else tick.ask)
                if side == "buy" and new_sl >= current_price:
                    continue
                if side == "sell" and new_sl <= current_price:
                    continue
                broker_safe = _minimum_safe_stop(
                    position_symbol,
                    side,
                    current_price,
                    new_sl,
                    current_spread_points(position_symbol),
                    cfg.signal_sl_min_points,
                )
                if (side == "buy" and broker_safe < new_sl - 0.005) or (
                    side == "sell" and broker_safe > new_sl + 0.005
                ):
                    continue
                new_sl = broker_safe
                result = modify_position(position, sl=new_sl, tp=execution_tp)
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "group_tp1_be_attempt",
                        "signal_id": signal_id,
                        "group_key": group_key,
                        "position_ticket": int(getattr(position, "ticket", 0) or 0),
                        "current_price": current_price,
                        "new_sl": new_sl,
                        "tp": execution_tp,
                        "retcode": retcode,
                    },
                )
                if retcode in {10008, 10009}:
                    managed["group_tp1_protected"] = True
                    managed["group_tp1_protected_utc"] = now.isoformat()
                    managed["last_sl"] = float(new_sl)
                    changed = True
                    log.info(f"[PROTECT] group TP1 moved SL to BE for {signal_id}: sl={new_sl} retcode={retcode}")
        return changed

    def _manage_phoenix_martingale(signals: dict, now: datetime) -> bool:
        if not _env_bool("PHOENIX_MARTINGALE_ENABLED", False):
            return False
        adverse_usd = max(0.1, _env_float("PHOENIX_MARTINGALE_ADVERSE_USD", 1.5))
        multiplier = max(1.0, _env_float("PHOENIX_MARTINGALE_MULTIPLIER", 2.0))
        max_add_legs = max(1, int(_env_float("PHOENIX_MARTINGALE_MAX_ADD_LEGS", 3.0)))
        max_group_positions = max(max_add_legs + 1, int(_env_float("PHOENIX_MARTINGALE_MAX_GROUP_POSITIONS", 12.0)))

        position_matches: list[tuple[object, str, dict]] = []
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                comment = str(getattr(position, "comment", "") or "")
                if "MG1" in comment:
                    continue
                for signal_id, managed in list(signals.items()):
                    if not _is_phoenix_source(managed.get("chat_id"), str(managed.get("chat_title", "") or "")):
                        continue
                    if str(managed.get("phoenix_martingale_role", "") or "") == "mg1":
                        continue
                    if _position_matches_managed(position, managed):
                        position_matches.append((position, signal_id, managed))
                        break

        groups: dict[str, list[tuple[object, str, dict]]] = {}
        for item in position_matches:
            groups.setdefault(_managed_message_key(item[2]), []).append(item)

        changed = False
        for group_key, items in groups.items():
            if not items or any(bool(item[2].get("phoenix_martingale_step_1_done")) for item in items):
                continue
            if len(items) >= max_group_positions:
                continue

            sides = {
                "buy" if int(getattr(position, "type", -1)) == int(mt5.POSITION_TYPE_BUY) else "sell"
                for position, _signal_id, _managed in items
            }
            if len(sides) != 1:
                continue
            side = next(iter(sides))
            symbol_name = str(getattr(items[0][0], "symbol", "") or items[0][2].get("symbol", symbol_by_asset["gold"]))
            tick = get_tick(symbol_name)
            current_price = float(tick.bid if side == "buy" else tick.ask)
            average_entry = sum(float(getattr(position, "price_open", 0.0) or 0.0) for position, _sid, _m in items) / len(items)
            adverse_move = average_entry - current_price if side == "buy" else current_price - average_entry
            if adverse_move < adverse_usd:
                continue

            source_items = items[: min(max_add_legs, max(1, max_group_positions - len(items)))]
            opened: list[dict] = []
            for index, (source_position, source_signal_id, source_managed) in enumerate(source_items, start=1):
                sl = float(source_managed.get("last_sl", 0.0) or source_managed.get("initial_sl", 0.0) or getattr(source_position, "sl", 0.0) or 0.0)
                tp = float(source_managed.get("execution_tp", 0.0) or getattr(source_position, "tp", 0.0) or 0.0)
                if not _valid_stop_for_side(side, current_price, sl) or tp <= 0.0:
                    _append_jsonl(
                        events_path,
                        {
                            "type": "phoenix_martingale_skip_bad_levels",
                            "group_key": group_key,
                            "source_signal_id": source_signal_id,
                            "side": side,
                            "current_price": current_price,
                            "sl": sl,
                            "tp": tp,
                        },
                    )
                    continue
                volume = normalize_volume(
                    symbol_name,
                    float(getattr(source_position, "volume", 0.0) or 0.0) * multiplier,
                    cfg.min_lot,
                    max(cfg.max_lot, cfg.signal_dynamic_lot_max),
                )
                result = send_market_order(
                    symbol=symbol_name,
                    side=side,
                    volume=volume,
                    sl=0.0 if _delay_broker_levels_for_min_hold() else sl,
                    tp=0.0 if _delay_broker_levels_for_min_hold() else tp,
                    deviation=cfg.deviation,
                    magic=cfg.magic,
                    comment=f"PHOENIX:MG1 x{multiplier:.0f}"[:31],
                )
                retcode = getattr(result, "retcode", None)
                order_ticket = int(getattr(result, "order", 0) or 0)
                deal_ticket = int(getattr(result, "deal", 0) or 0)
                opened.append(
                    {
                        "source_signal_id": source_signal_id,
                        "volume": volume,
                        "sl": sl,
                        "tp": tp,
                        "retcode": retcode,
                        "order_ticket": order_ticket,
                        "deal_ticket": deal_ticket,
                    }
                )
                if retcode in {10008, 10009}:
                    managed_id = f"{group_key}:phoenix-mg1:{order_ticket or deal_ticket or index}"
                    signals[managed_id] = {
                        **source_managed,
                        "signal_id": managed_id,
                        "order_kind": "market",
                        "order_ticket": order_ticket,
                        "deal_ticket": deal_ticket,
                        "entry": current_price,
                        "initial_sl": sl,
                        "execution_tp": tp,
                        "created_utc": now.isoformat(),
                        "phoenix_martingale_role": "mg1",
                        "phoenix_martingale_source_signal_id": source_signal_id,
                        "phoenix_martingale_multiplier": multiplier,
                        "protected_to_tp1": False,
                    }
                    changed = True

            if opened:
                for _position, _signal_id, managed in items:
                    managed["phoenix_martingale_step_1_done"] = True
                    managed["phoenix_martingale_step_1_utc"] = now.isoformat()
                    managed["phoenix_martingale_adverse_move"] = round(adverse_move, 2)
                changed = True
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_martingale_step_1",
                        "group_key": group_key,
                        "side": side,
                        "symbol": symbol_name,
                        "average_entry": round(average_entry, 2),
                        "current_price": round(current_price, 2),
                        "adverse_usd": adverse_usd,
                        "multiplier": multiplier,
                        "opened": opened,
                    },
                )
                log.info(
                    f"[PHOENIX-MG] group={group_key} adverse={adverse_move:.2f} "
                    f"opened={sum(1 for row in opened if row.get('retcode') in {10008, 10009})}/{len(opened)}"
                )
        return changed

    def _manage_min_hold_position_exits(signals: dict, now: datetime) -> bool:
        changed = False
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                matched = None
                for signal_id, managed in list(signals.items()):
                    if _position_matches_managed(position, managed):
                        matched = (signal_id, managed)
                        break
                if matched is None:
                    continue
                signal_id, managed = matched
                remaining = _hold_remaining_seconds(position, managed)
                if remaining > 0:
                    continue
                side = str(managed.get("side", "") or "")
                if side not in {"buy", "sell"}:
                    continue
                tick = get_tick(managed_symbol)
                current_price = float(tick.bid if side == "buy" else tick.ask)
                intended_sl = float(managed.get("last_sl", 0.0) or managed.get("edited_sl", 0.0) or managed.get("initial_sl", 0.0) or 0.0)
                intended_tp = float(managed.get("edited_tp", 0.0) or managed.get("execution_tp", 0.0) or 0.0)
                deferred_hit = int(managed.get("deferred_channel_tp_hit_level", 0) or 0)
                target_level = _managed_target_level(managed)
                hit_deferred_channel_tp = deferred_hit >= 99 or (deferred_hit > 0 and target_level <= deferred_hit)
                exit_reason = _manual_exit_reason_after_hold(
                    side=side,
                    current_price=current_price,
                    intended_sl=intended_sl,
                    intended_tp=intended_tp,
                    broker_sl=float(getattr(position, "sl", 0.0) or 0.0),
                    broker_tp=float(getattr(position, "tp", 0.0) or 0.0),
                    deferred_channel_tp_hit=hit_deferred_channel_tp,
                )
                if exit_reason:
                    result = close_position(position, deviation=cfg.deviation)
                    retcode = getattr(result, "retcode", None)
                    _append_jsonl(
                        events_path,
                        {
                            "type": "min_hold_manual_exit_attempt",
                            "signal_id": signal_id,
                            "position_ticket": int(getattr(position, "ticket", 0) or 0),
                            "reason": exit_reason,
                            "current_price": current_price,
                            "intended_sl": intended_sl,
                            "intended_tp": intended_tp,
                            "broker_sl": float(getattr(position, "sl", 0.0) or 0.0),
                            "broker_tp": float(getattr(position, "tp", 0.0) or 0.0),
                            "age_seconds": round(_position_age_seconds(position), 1),
                            "min_hold_seconds": round(float(managed.get("min_hold_seconds", 0.0) or 0.0), 1),
                            "retcode": retcode,
                        },
                    )
                    if retcode in {10008, 10009}:
                        managed["min_hold_closed"] = True
                        managed["min_hold_closed_utc"] = now.isoformat()
                        managed["min_hold_close_reason"] = exit_reason
                    changed = True
                    continue
                if _delay_broker_levels_for_min_hold() and (
                    float(getattr(position, "sl", 0.0) or 0.0) <= 0.0
                    or float(getattr(position, "tp", 0.0) or 0.0) <= 0.0
                ):
                    result = modify_position(position, sl=intended_sl, tp=intended_tp)
                    retcode = getattr(result, "retcode", None)
                    _append_jsonl(
                        events_path,
                        {
                            "type": "min_hold_levels_armed",
                            "signal_id": signal_id,
                            "position_ticket": int(getattr(position, "ticket", 0) or 0),
                            "sl": intended_sl,
                            "tp": intended_tp,
                            "retcode": retcode,
                        },
                    )
                    if retcode in {10008, 10009}:
                        managed["min_hold_levels_armed"] = True
                        managed["min_hold_levels_armed_utc"] = now.isoformat()
                    changed = True
        return changed

    def _manage_stale_profit_exits(signals: dict, now: datetime) -> bool:
        if not _env_bool("SIGNAL_STALE_PROFIT_EXIT_ENABLED", False):
            return False
        stale_minutes = max(1.0, _env_float("SIGNAL_STALE_PROFIT_EXIT_MINUTES", 30.0))
        min_profit = max(0.0, _env_float("SIGNAL_STALE_PROFIT_EXIT_MIN_USD", 0.01))
        changed = False
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                matched = None
                for signal_id, managed in list(signals.items()):
                    if _position_matches_managed(position, managed):
                        matched = (signal_id, managed)
                        break
                if matched is None:
                    continue
                signal_id, managed = matched
                if _hold_remaining_seconds(position, managed) > 0:
                    continue
                side = str(managed.get("side", "") or "").lower()
                if side not in {"buy", "sell"}:
                    continue
                tick = get_tick(managed_symbol)
                current_price = float(tick.bid if side == "buy" else tick.ask)
                should_close, state_changed, reached_level = _stale_profit_exit_update(
                    managed,
                    side=side,
                    current_price=current_price,
                    floating_profit=float(getattr(position, "profit", 0.0) or 0.0),
                    now=now,
                    stale_minutes=stale_minutes,
                    min_profit=min_profit,
                )
                changed = changed or state_changed
                if not should_close:
                    continue
                result = close_position(position, deviation=cfg.deviation)
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "stale_profit_exit_attempt",
                        "signal_id": signal_id,
                        "position_ticket": int(getattr(position, "ticket", 0) or 0),
                        "current_price": current_price,
                        "floating_profit": float(getattr(position, "profit", 0.0) or 0.0),
                        "reached_tp_level": int(managed.get("live_exit_reached_tp_level", reached_level) or reached_level),
                        "stale_minutes": stale_minutes,
                        "retcode": retcode,
                    },
                )
                if retcode in {10008, 10009}:
                    managed["stale_profit_closed"] = True
                    managed["stale_profit_closed_utc"] = now.isoformat()
                    managed["stale_profit_close_retcode"] = retcode
                    changed = True
                    log.info(
                        f"[LIVE-EXIT] closed stale profitable leg for {signal_id}: "
                        f"profit={float(getattr(position, 'profit', 0.0) or 0.0):.2f} "
                        f"last_tp={int(managed.get('live_exit_reached_tp_level', reached_level) or reached_level)} "
                        f"retcode={retcode}"
                    )
                else:
                    log.warning(f"[LIVE-EXIT] stale profit close rejected for {signal_id}: retcode={retcode}")
        return changed

    channel_analysis_last_epoch = 0.0

    def _protect_phoenix_range_positions(now: datetime) -> int:
        if not _env_bool("PHOENIX_RANGE_TRIGGER_BE_ENABLED", True):
            return 0
        trigger_usd = max(0.1, _env_float("PHOENIX_RANGE_TRIGGER_BE_TRIGGER_USD", 0.50))
        buffer_usd = max(0.0, _env_float("PHOENIX_RANGE_TRIGGER_BE_BUFFER_USD", 0.25))
        changed_count = 0
        for managed_symbol in _active_symbols():
            spread_points = current_spread_points(managed_symbol)
            tick = get_tick(managed_symbol)
            for position in positions_by_magic(managed_symbol, cfg.magic):
                comment = str(getattr(position, "comment", "") or "")
                if not comment.startswith(("PHOENIX:RANGE", "PHOENIX:DIR RUNN")):
                    continue
                position_trigger_usd = trigger_usd
                position_buffer_usd = buffer_usd
                if comment.startswith("PHOENIX:RANGE OPT"):
                    position_trigger_usd = max(
                        0.1,
                        _env_float("PHOENIX_RANGE_OPTIMIZED_EXTRA_BE_TRIGGER_USD", 0.75),
                    )
                    position_buffer_usd = max(
                        0.0,
                        _env_float("PHOENIX_RANGE_OPTIMIZED_EXTRA_BE_BUFFER_USD", 0.25),
                    )
                side = "buy" if int(getattr(position, "type", -1)) == int(mt5.POSITION_TYPE_BUY) else "sell"
                entry = float(getattr(position, "price_open", 0.0) or 0.0)
                current_sl = float(getattr(position, "sl", 0.0) or 0.0)
                current_price = float(tick.bid if side == "buy" else tick.ask)
                candidate = _phoenix_range_be_candidate(
                    side,
                    entry,
                    current_price,
                    current_sl,
                    position_trigger_usd,
                    position_buffer_usd,
                )
                if candidate is None:
                    continue
                broker_safe = _minimum_safe_stop(
                    managed_symbol,
                    side,
                    current_price,
                    candidate,
                    spread_points,
                    cfg.signal_sl_min_points,
                )
                if (side == "buy" and broker_safe < candidate - 0.005) or (side == "sell" and broker_safe > candidate + 0.005):
                    continue
                result = modify_position(
                    position,
                    sl=broker_safe,
                    tp=float(getattr(position, "tp", 0.0) or 0.0),
                )
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_range_be_attempt",
                        "position_ticket": int(getattr(position, "ticket", 0) or 0),
                        "side": side,
                        "entry": entry,
                        "current_price": current_price,
                        "old_sl": current_sl,
                        "new_sl": broker_safe,
                        "trigger_usd": position_trigger_usd,
                        "buffer_usd": position_buffer_usd,
                        "retcode": retcode,
                        "time": now.isoformat(),
                    },
                )
                if retcode in {10008, 10009}:
                    changed_count += 1
                    log.info(
                        f"[PHOENIX-RANGE] protected ticket={int(getattr(position, 'ticket', 0) or 0)} "
                        f"at entry+buffer: sl={broker_safe:.2f} "
                        f"trigger={position_trigger_usd:.2f} buffer={position_buffer_usd:.2f}"
                    )
                else:
                    log.warning(
                        f"[PHOENIX-RANGE] BE modify rejected ticket={int(getattr(position, 'ticket', 0) or 0)} "
                        f"retcode={retcode}"
                    )
        return changed_count

    def _reconcile_phoenix_preliminary_positions(signal: ParsedSignal) -> int:
        if not _env_bool("PHOENIX_PRELIMINARY_RECONCILE_ENABLED", True):
            return 0
        symbol_name = _symbol_for_signal(signal)
        if not symbol_name:
            return 0
        adverse_gap = max(0.0, _env_float("PHOENIX_PRELIMINARY_RECONCILE_GAP_USD", 2.0))
        actions = 0
        for position in positions_by_magic(symbol_name, cfg.magic):
            comment = str(getattr(position, "comment", "") or "")
            if not (comment.startswith("PHOENIX:DIR RUNN") or comment.startswith("PHOENIX:RANGE")):
                continue
            position_side = (
                "buy"
                if int(getattr(position, "type", -1)) == int(mt5.POSITION_TYPE_BUY)
                else "sell"
            )
            reason = _phoenix_preliminary_reconcile_reason(
                position_side,
                float(getattr(position, "price_open", 0.0) or 0.0),
                float(getattr(position, "profit", 0.0) or 0.0),
                signal.side,
                signal.entries,
                adverse_gap,
            )
            if not reason:
                continue
            result = close_position(position, cfg.deviation)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_preliminary_reconcile_close_attempt",
                    "reason": reason,
                    "position_ticket": int(getattr(position, "ticket", 0) or 0),
                    "position_side": position_side,
                    "entry": float(getattr(position, "price_open", 0.0) or 0.0),
                    "profit": float(getattr(position, "profit", 0.0) or 0.0),
                    "full_signal": signal.as_dict(),
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                actions += 1
                runners = state.get("phoenix_direction_runner_positions", {})
                if isinstance(runners, dict):
                    runners.pop(str(int(getattr(position, "ticket", 0) or 0)), None)
                log.info(
                    f"[PHOENIX-RECONCILE] closed preliminary position "
                    f"ticket={int(getattr(position, 'ticket', 0) or 0)} reason={reason}"
                )
            else:
                log.warning(
                    f"[PHOENIX-RECONCILE] close rejected "
                    f"ticket={int(getattr(position, 'ticket', 0) or 0)} retcode={retcode}"
                )
        for order in orders_by_magic(symbol_name, cfg.magic):
            comment = str(getattr(order, "comment", "") or "")
            if not comment.startswith("PHOENIX:RANGE"):
                continue
            order_type = int(getattr(order, "type", -1))
            order_side = (
                "buy"
                if order_type in {
                    int(mt5.ORDER_TYPE_BUY_LIMIT),
                    int(mt5.ORDER_TYPE_BUY_STOP),
                    int(mt5.ORDER_TYPE_BUY_STOP_LIMIT),
                }
                else "sell"
            )
            reason = _phoenix_preliminary_reconcile_reason(
                order_side,
                float(getattr(order, "price_open", 0.0) or 0.0),
                -0.01,
                signal.side,
                signal.entries,
                adverse_gap,
            )
            if not reason:
                continue
            result = remove_order(order, magic=cfg.magic)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_preliminary_reconcile_pending_remove_attempt",
                    "reason": reason,
                    "order_ticket": int(getattr(order, "ticket", 0) or 0),
                    "order_side": order_side,
                    "entry": float(getattr(order, "price_open", 0.0) or 0.0),
                    "full_signal": signal.as_dict(),
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                actions += 1
                log.info(
                    f"[PHOENIX-RECONCILE] removed preliminary pending "
                    f"ticket={int(getattr(order, 'ticket', 0) or 0)} reason={reason}"
                )
            else:
                log.warning(
                    f"[PHOENIX-RECONCILE] pending remove rejected "
                    f"ticket={int(getattr(order, 'ticket', 0) or 0)} retcode={retcode}"
                )
        if actions:
            _save_runtime_state()
        return actions

    def _expire_losing_phoenix_direction_runners(now: datetime) -> int:
        max_hold_minutes = max(0.0, _env_float("PHOENIX_DIRECTION_RUNNER_MAX_HOLD_MINUTES", 10.0))
        if max_hold_minutes <= 0:
            return 0
        records = state.get("phoenix_direction_runner_positions", {})
        if not isinstance(records, dict) or not records:
            return 0
        positions = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions[int(getattr(position, "ticket", 0) or 0)] = position
        closed = 0
        changed = False
        for ticket_text, record in list(records.items()):
            ticket = int(ticket_text or 0)
            position = positions.get(ticket)
            if position is None:
                records.pop(ticket_text, None)
                changed = True
                continue
            try:
                opened = datetime.fromisoformat(str(record.get("opened_utc", "")).replace("Z", "+00:00"))
            except Exception:
                opened = now
            age_seconds = max(0.0, (now - opened).total_seconds())
            profit = float(getattr(position, "profit", 0.0) or 0.0)
            hard_timeout = _env_bool("PHOENIX_DIRECTION_RUNNER_HARD_TIMEOUT", False)
            timeout_due = age_seconds >= max_hold_minutes * 60.0 and (hard_timeout or profit < 0.0)
            if not timeout_due:
                continue
            result = close_position(position, cfg.deviation)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_direction_runner_timeout_close_attempt",
                    "position_ticket": ticket,
                    "age_seconds": age_seconds,
                    "profit": profit,
                    "max_hold_minutes": max_hold_minutes,
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                records.pop(ticket_text, None)
                changed = True
                closed += 1
                log.info(
                    f"[PHOENIX DIR RUNNER] closed timed runner after "
                    f"{age_seconds / 60.0:.1f}m ticket={ticket} profit={profit:.2f}"
                )
            else:
                log.warning(f"[PHOENIX DIR RUNNER] timeout close rejected ticket={ticket} retcode={retcode}")
        if changed:
            state["phoenix_direction_runner_positions"] = records
            _save_runtime_state()
        return closed

    def _expire_phoenix_range_pending_orders(now: datetime) -> int:
        records = state.get("phoenix_range_pending_orders", [])
        if not isinstance(records, list) or not records:
            return 0
        active_orders = {}
        for managed_symbol in _active_symbols():
            for order in orders_by_magic(managed_symbol, cfg.magic):
                active_orders[int(getattr(order, "ticket", 0) or 0)] = order
        kept = []
        removed = 0
        changed = False
        for record in records:
            ticket = int(record.get("order_ticket", 0) or 0)
            order = active_orders.get(ticket)
            if order is None:
                changed = True
                continue
            try:
                expires = datetime.fromisoformat(str(record.get("expires_utc", "")).replace("Z", "+00:00"))
            except Exception:
                expires = now
            if now < expires:
                kept.append(record)
                continue
            result = remove_order(order, magic=cfg.magic)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_range_pending_expiry_attempt",
                    "order_ticket": ticket,
                    "expires_utc": expires.isoformat(),
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                removed += 1
                changed = True
                log.info(f"[PHOENIX-RANGE] expired pending removed ticket={ticket} retcode={retcode}")
            else:
                kept.append(record)
                log.warning(f"[PHOENIX-RANGE] pending expiry remove rejected ticket={ticket} retcode={retcode}")
        if changed:
            state["phoenix_range_pending_orders"] = kept[-500:]
            _save_runtime_state()
        return removed

    def _cancel_phoenix_range_pending_for_chat(
        chat_id: int | None,
        reason: str,
        source_message_id: int = 0,
    ) -> int:
        records = state.get("phoenix_range_pending_orders", [])
        if not isinstance(records, list) or not records:
            return 0
        requested_chat_id = int(chat_id or 0)
        active_orders = {
            int(getattr(order, "ticket", 0) or 0): order
            for managed_symbol in _active_symbols()
            for order in orders_by_magic(managed_symbol, cfg.magic)
        }
        kept = []
        removed = 0
        changed = False
        for record in records:
            ticket = int(record.get("order_ticket", 0) or 0)
            order = active_orders.get(ticket)
            if order is None:
                changed = True
                continue
            record_chat_id = int(record.get("chat_id", 0) or 0)
            if requested_chat_id and record_chat_id and record_chat_id != requested_chat_id:
                kept.append(record)
                continue
            result = remove_order(order, magic=cfg.magic)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_range_pending_cancel_attempt",
                    "order_ticket": ticket,
                    "chat_id": requested_chat_id,
                    "source_message_id": int(source_message_id or 0),
                    "reason": reason,
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                removed += 1
                changed = True
                log.info(
                    f"[PHOENIX-RANGE] pending removed ticket={ticket} "
                    f"reason={reason} retcode={retcode}"
                )
            else:
                kept.append(record)
                log.warning(
                    f"[PHOENIX-RANGE] pending cancel rejected ticket={ticket} "
                    f"reason={reason} retcode={retcode}"
                )
        if changed:
            state["phoenix_range_pending_orders"] = kept[-500:]
            _save_runtime_state()
        return removed

    def _sync_phoenix_range_provider_sl(signal: ParsedSignal) -> int:
        if not _is_phoenix_source(signal.chat_id, signal.chat_title) or signal.sl <= 0:
            return 0
        changed = 0
        provider_sl = float(signal.sl)
        provider_sl_cap = max(0.0, _env_float("PHOENIX_PROVIDER_SL_MAX_DISTANCE_USD", 12.0))
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                position_comment = str(getattr(position, "comment", "") or "")
                if not position_comment.startswith(("PHOENIX:RANGE", "PHOENIX:DIR")):
                    continue
                if position_comment.startswith("PHOENIX:RANGE OPT"):
                    continue
                position_side = "buy" if int(getattr(position, "type", -1)) == int(mt5.POSITION_TYPE_BUY) else "sell"
                entry = float(getattr(position, "price_open", 0.0) or 0.0)
                current_sl = float(getattr(position, "sl", 0.0) or 0.0)
                if position_side != signal.side or not _valid_stop_for_side(signal.side, entry, provider_sl):
                    continue
                effective_sl = _phoenix_provider_stop_with_cap(signal.side, entry, provider_sl, provider_sl_cap)
                if (
                    _env_bool("PHOENIX_PRELIMINARY_KEEP_TIGHTER_SL", True)
                    and _valid_stop_for_side(signal.side, entry, current_sl)
                ):
                    effective_sl = (
                        max(current_sl, effective_sl)
                        if signal.side == "buy"
                        else min(current_sl, effective_sl)
                    )
                already_protected = current_sl > 0 and (
                    (signal.side == "buy" and current_sl >= entry)
                    or (signal.side == "sell" and current_sl <= entry)
                )
                if already_protected or abs(current_sl - effective_sl) < 0.01:
                    continue
                result = modify_position(position, sl=effective_sl, tp=float(getattr(position, "tp", 0.0) or 0.0))
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_range_provider_sl_sync_attempt",
                        "target": "position",
                        "ticket": int(getattr(position, "ticket", 0) or 0),
                        "old_sl": current_sl,
                        "new_sl": effective_sl,
                        "retcode": retcode,
                    },
                )
                if retcode in {10008, 10009}:
                    changed += 1
                    log.info(
                        f"[PHOENIX-RANGE] synced provider SL on position "
                        f"ticket={int(getattr(position, 'ticket', 0) or 0)} sl={current_sl:.2f}->{effective_sl:.2f}"
                    )
            for order in orders_by_magic(managed_symbol, cfg.magic):
                order_comment = str(getattr(order, "comment", "") or "")
                if not order_comment.startswith(("PHOENIX:RANGE", "PHOENIX:DIR")):
                    continue
                if order_comment.startswith("PHOENIX:RANGE OPT"):
                    continue
                order_type = int(getattr(order, "type", -1))
                order_side = "buy" if order_type in {int(mt5.ORDER_TYPE_BUY_LIMIT), int(mt5.ORDER_TYPE_BUY_STOP)} else "sell"
                entry = float(getattr(order, "price_open", 0.0) or 0.0)
                current_sl = float(getattr(order, "sl", 0.0) or 0.0)
                if order_side != signal.side or not _valid_stop_for_side(signal.side, entry, provider_sl):
                    continue
                effective_sl = _phoenix_provider_stop_with_cap(signal.side, entry, provider_sl, provider_sl_cap)
                if (
                    _env_bool("PHOENIX_PRELIMINARY_KEEP_TIGHTER_SL", True)
                    and _valid_stop_for_side(signal.side, entry, current_sl)
                ):
                    effective_sl = (
                        max(current_sl, effective_sl)
                        if signal.side == "buy"
                        else min(current_sl, effective_sl)
                    )
                if abs(current_sl - effective_sl) < 0.01:
                    continue
                result = modify_order(
                    order,
                    price=entry,
                    sl=effective_sl,
                    tp=float(getattr(order, "tp", 0.0) or 0.0),
                    magic=cfg.magic,
                )
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_range_provider_sl_sync_attempt",
                        "target": "pending",
                        "ticket": int(getattr(order, "ticket", 0) or 0),
                        "old_sl": current_sl,
                        "new_sl": effective_sl,
                        "retcode": retcode,
                    },
                )
                if retcode in {10008, 10009}:
                    changed += 1
                    log.info(
                        f"[PHOENIX-RANGE] synced provider SL on pending "
                        f"ticket={int(getattr(order, 'ticket', 0) or 0)} sl={current_sl:.2f}->{effective_sl:.2f}"
                    )
        return changed

    async def _manage_open_positions() -> None:
        nonlocal channel_analysis_last_epoch
        while True:
            try:
                if await _stop_if_session_limit_reached():
                    return
                signals = managed_state.setdefault("signals", {})
                now = datetime.now(UTC)
                changed = False
                _protect_phoenix_range_positions(now)
                _expire_phoenix_range_pending_orders(now)
                _expire_losing_phoenix_direction_runners(now)
                _scan_closed_trades_for_learning(now)
                analysis_interval = max(15.0, _env_float("SIGNAL_CHANNEL_ANALYSIS_INTERVAL_SECONDS", 60.0))
                if time.time() - channel_analysis_last_epoch >= analysis_interval:
                    terminal_status = trading_status()
                    _write_json(cfg.data_dir / "telegram_runtime_status.json", {
                        "heartbeat_utc": now.isoformat(),
                        "terminal": terminal_status,
                        "telegram_connected": client.is_connected(),
                    })
                    if not terminal_status["ready"]:
                        log.warning("[MT5-NOT-READY] %s", ",".join(terminal_status["blocked_reasons"]))
                    write_channel_report(
                        channel_analysis_path,
                        events_path,
                        state,
                        cfg.telegram_watch_channels,
                        window_days=max(1, int(_env_float("SIGNAL_CHANNEL_ANALYSIS_WINDOW_DAYS", 7.0))),
                    )
                    channel_analysis_last_epoch = time.time()
                if _manage_min_hold_position_exits(signals, now):
                    _save_managed_state(managed_path, managed_state)
                if _manage_stale_profit_exits(signals, now):
                    _save_managed_state(managed_path, managed_state)
                if _manage_phoenix_martingale(signals, now):
                    _save_managed_state(managed_path, managed_state)
                for managed_symbol in _active_symbols():
                    orders = orders_by_magic(managed_symbol, cfg.magic)
                    for order in orders:
                        ticket = int(getattr(order, "ticket", 0) or 0)
                        managed_match = None
                        for signal_id, managed in list(signals.items()):
                            try:
                                if int(managed.get("order_ticket", 0) or 0) == ticket:
                                    managed_match = (signal_id, managed)
                                    break
                            except Exception:
                                continue
                        if managed_match is None:
                            continue
                        signal_id, managed = managed_match
                        side = str(managed.get("side", "") or "")
                        tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
                        try:
                            created = datetime.fromisoformat(str(managed.get("created_utc", "")).replace("Z", "+00:00"))
                        except Exception:
                            created = now
                        age_minutes = (now - created).total_seconds() / 60.0
                        expiry_minutes = _pending_expiry_minutes(cfg, managed)
                        if str(managed.get("order_kind", "")).lower() in {"limit", "stop"} and side in {"buy", "sell"} and tps:
                            tick = get_tick(managed_symbol)
                            current_price = float(tick.bid if side == "buy" else tick.ask)
                            cancel_tp_index = _pending_cancel_tp_index(managed)
                            reached_tp_index = 0
                            for tp_index, tp_level in enumerate(tps[: max(2, cancel_tp_index)], start=1):
                                if _price_reached_tp(side, current_price, tp_level):
                                    reached_tp_index = tp_index
                            if _pending_cancel_level_reached(managed, current_price):
                                reached_tp_index = max(reached_tp_index, cancel_tp_index)
                            order_kind = str(managed.get("order_kind", "")).lower()
                            price_cancellation_allowed = _pending_price_cancellation_allowed(managed)
                            if price_cancellation_allowed and reached_tp_index >= cancel_tp_index:
                                result = remove_order(order, magic=cfg.magic)
                                retcode = getattr(result, "retcode", None)
                                _append_jsonl(
                                    events_path,
                                    {
                                        "type": "pending_tp_reached_remove_attempt",
                                        "signal_id": signal_id,
                                        "order_ticket": ticket,
                                        "symbol": managed_symbol,
                                        "side": side,
                                        "current_price": current_price,
                                        "reached_tp_index": reached_tp_index,
                                        "cancel_tp_index": cancel_tp_index,
                                        "retcode": retcode,
                                        "managed": managed,
                                    },
                                )
                                if retcode in {10008, 10009}:
                                    managed["tp_reached_removed"] = True
                                    managed["tp_reached_removed_utc"] = now.isoformat()
                                    managed["tp_reached_remove_retcode"] = retcode
                                    managed["tp_reached_index"] = reached_tp_index
                                    changed = True
                                    log.info(f"[CANCEL] removed pending {ticket} after TP{reached_tp_index} reached for {signal_id}: retcode={retcode}")
                                    continue
                                log.warning(f"[CANCEL] TP reached pending remove rejected {ticket} for {signal_id}: retcode={retcode}")
                            moved_away, away_distance, away_threshold = (False, 0.0, 0.0)
                            if price_cancellation_allowed:
                                moved_away, away_distance, away_threshold = _pending_limit_moved_away(
                                    managed_symbol,
                                    managed,
                                    current_price,
                                    cfg.signal_sl_min_points,
                                    age_minutes,
                                    expiry_minutes,
                                )
                            if moved_away:
                                result = remove_order(order, magic=cfg.magic)
                                retcode = getattr(result, "retcode", None)
                                _append_jsonl(
                                    events_path,
                                    {
                                        "type": "pending_moved_away_remove_attempt",
                                        "signal_id": signal_id,
                                        "order_ticket": ticket,
                                        "symbol": managed_symbol,
                                        "side": side,
                                        "current_price": current_price,
                                        "entry": float(managed.get("entry", 0.0) or 0.0),
                                        "away_distance": round(away_distance, 2),
                                        "away_threshold": round(away_threshold, 2),
                                        "retcode": retcode,
                                        "managed": managed,
                                    },
                                )
                                if retcode in {10008, 10009}:
                                    managed["moved_away_removed"] = True
                                    managed["moved_away_removed_utc"] = now.isoformat()
                                    managed["moved_away_remove_retcode"] = retcode
                                    managed["moved_away_distance"] = round(away_distance, 2)
                                    managed["moved_away_threshold"] = round(away_threshold, 2)
                                    changed = True
                                    log.info(
                                        f"[CANCEL] removed pending {ticket} after price moved away for {signal_id}: "
                                        f"distance={away_distance:.2f} threshold={away_threshold:.2f} retcode={retcode}"
                                    )
                                    continue
                                log.warning(f"[CANCEL] moved-away pending remove rejected {ticket} for {signal_id}: retcode={retcode}")
                        if age_minutes < expiry_minutes:
                            continue
                        result = remove_order(order, magic=cfg.magic)
                        retcode = getattr(result, "retcode", None)
                        _append_jsonl(
                            events_path,
                            {
                                "type": "pending_expiry_remove_attempt",
                                "signal_id": signal_id,
                                "order_ticket": ticket,
                                "symbol": managed_symbol,
                                "age_minutes": round(age_minutes, 1),
                                "expiry_minutes": expiry_minutes,
                                "retcode": retcode,
                                "managed": managed,
                            },
                        )
                        if retcode in {10008, 10009}:
                            managed["expired_removed"] = True
                            managed["expired_removed_utc"] = now.isoformat()
                            managed["expired_remove_retcode"] = retcode
                            changed = True
                            log.info(f"[EXPIRE] removed stale pending {ticket} for {signal_id}: age={age_minutes:.1f}m retcode={retcode}")
                        else:
                            log.warning(f"[EXPIRE] pending remove rejected {ticket} for {signal_id}: age={age_minutes:.1f}m retcode={retcode}")
                if changed:
                    _save_managed_state(managed_path, managed_state)
                if cfg.signal_protect_tp1_enabled:
                    if _protect_groups_after_tp1(signals, now):
                        _save_managed_state(managed_path, managed_state)
                    changed = False
                    positions = []
                    for managed_symbol in _active_symbols():
                        positions.extend(positions_by_magic(managed_symbol, cfg.magic))
                    for position in positions:
                        for signal_id, managed in list(signals.items()):
                            if len(managed.get("tps", [])) < 2:
                                continue
                            if not _position_matches_managed(position, managed):
                                continue
                            if _hold_remaining_seconds(position, managed) > 0:
                                continue
                            protect_mode = str(managed.get("protect_mode", "tp1") or "tp1").lower()
                            if protect_mode == "none":
                                continue
                            if protect_mode in {"be", "tp1", "be_after_tp2", "be_after_tp3", "tp1_after_tp3"} and managed.get("protected_to_tp1"):
                                continue
                            side = str(managed.get("side", ""))
                            entry = float(managed.get("entry", 0.0) or getattr(position, "price_open", 0.0) or 0.0)
                            tp1 = float(managed.get("tp1", 0.0) or 0.0)
                            execution_tp = float(managed.get("execution_tp", 0.0) or getattr(position, "tp", 0.0) or 0.0)
                            if entry <= 0 or tp1 <= 0:
                                continue
                            current_sl = float(getattr(position, "sl", 0.0) or 0.0)
                            if protect_mode == "tp1" and _sl_already_protected(side, current_sl, tp1):
                                managed["protected_to_tp1"] = True
                                managed["protected_utc"] = datetime.now(UTC).isoformat()
                                changed = True
                                continue
                            position_symbol = str(getattr(position, "symbol", "") or symbol_by_asset["gold"])
                            tick = get_tick(position_symbol)
                            current_price = float(tick.bid if side == "buy" else tick.ask)
                            if protect_mode in {"be_after_tp2", "tp1_after_tp3", "be_after_tp3"}:
                                tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
                                trigger_level = 2 if protect_mode == "be_after_tp2" else 3
                                if len(tps) < trigger_level:
                                    continue
                                trigger_price = float(tps[trigger_level - 1])
                                if not _price_reached_tp1_trigger(side, current_price, trigger_price):
                                    continue
                            elif protect_mode == "phoenix_ladder":
                                tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
                                reached_level = _phoenix_reached_tp_level(side, current_price, tps)
                                if reached_level <= 0:
                                    continue
                                trigger_price = float(tps[min(reached_level - 1, len(tps) - 1)])
                            else:
                                trigger_price = _trigger_price_for_tp1(side, entry, tp1, cfg.signal_protect_tp1_trigger_pct)
                                if not _price_reached_tp1_trigger(side, current_price, trigger_price):
                                    continue
                            if protect_mode == "be":
                                new_sl = _better_stop(side, current_sl, entry)
                            elif protect_mode == "be_after_tp2":
                                new_sl = _better_stop(side, current_sl, entry)
                            elif protect_mode == "be_after_tp3":
                                new_sl = _better_stop(side, current_sl, entry)
                            elif protect_mode == "atr":
                                candidate = _atr_trailing_stop(position_symbol, side, current_price, float(managed.get("atr_mult", 1.5) or 1.5))
                                if candidate is None:
                                    continue
                                new_sl = _better_stop(side, current_sl, candidate)
                            elif protect_mode == "tp1_after_tp3":
                                new_sl = _better_stop(side, current_sl, tp1)
                            elif protect_mode == "phoenix_ladder":
                                new_sl = _phoenix_progressive_stop(side, entry, [float(value) for value in managed.get("tps", [])], reached_level, current_sl)
                                if new_sl is None:
                                    continue
                            else:
                                new_sl = _better_stop(side, current_sl, tp1)
                            if current_sl > 0 and abs(new_sl - current_sl) < 0.01:
                                continue
                            result = modify_position(position, sl=new_sl, tp=execution_tp)
                            retcode = getattr(result, "retcode", None)
                            _append_jsonl(
                                events_path,
                                {
                                    "type": "protect_tp1_attempt",
                                    "protect_mode": protect_mode,
                                    "signal_id": signal_id,
                                    "position_ticket": int(getattr(position, "ticket", 0) or 0),
                                    "current_price": current_price,
                                    "trigger_price": trigger_price,
                                    "new_sl": new_sl,
                                    "tp": execution_tp,
                                    "retcode": retcode,
                                },
                            )
                            if retcode in {10008, 10009}:
                                if protect_mode in {"be", "tp1", "be_after_tp2", "be_after_tp3", "tp1_after_tp3"}:
                                    managed["protected_to_tp1"] = True
                                if protect_mode == "phoenix_ladder":
                                    managed["phoenix_ladder_level"] = int(reached_level)
                                managed["protected_utc"] = datetime.now(UTC).isoformat()
                                managed["protected_retcode"] = retcode
                                managed["last_sl"] = float(new_sl)
                                changed = True
                                log.info(f"[PROTECT] {protect_mode} moved SL for {signal_id}: sl={new_sl} tp={execution_tp} retcode={retcode}")
                            else:
                                log.warning(f"[PROTECT] SL move rejected for {signal_id}: retcode={retcode}")
                    if changed:
                        _save_managed_state(managed_path, managed_state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"[PROTECT] manager error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(1.0, float(cfg.loop_seconds)))

    async def _place_signal(signal: ParsedSignal, channel_username: str = "") -> None:
        any_success = False
        try:
            if not _channel_asset_allowed(signal.chat_id, signal.asset):
                log.info(
                    f"[SKIP] channel asset filtered: chat_id={signal.chat_id} asset={signal.asset}"
                )
                _append_jsonl(
                    events_path,
                    {
                        "type": "skip",
                        "reason": "channel_asset_filtered",
                        "asset": signal.asset,
                        "signal": signal.as_dict(),
                    },
                )
                return
            symbol_name = _symbol_for_signal(signal)
            if not symbol_name:
                log.info(f"[SKIP] unsupported asset on this MT5 account: {signal.asset}")
                _append_jsonl(events_path, {"type": "skip", "reason": "unsupported_asset_symbol", "asset": signal.asset, "signal": signal.as_dict()})
                return
            spread_points = current_spread_points(symbol_name)
            max_spread_points = float(ASSET_MAX_SPREAD_POINTS.get(signal.asset, cfg.max_spread_points))
            if spread_points > max_spread_points:
                log.info(f"[SKIP] spread too wide: {spread_points:.1f} > {max_spread_points:.1f}")
                _append_jsonl(events_path, {"type": "skip", "reason": "spread", "spread_points": spread_points, "max_spread_points": max_spread_points, "signal": signal.as_dict()})
                return
            paused, learning_key = _adaptive_channel_paused(signal)
            if paused:
                log.info(f"[LEARN] skipped paused channel {learning_key} for {signal.uid}")
                _append_jsonl(
                    events_path,
                    {
                        "type": "skip",
                        "reason": "adaptive_channel_paused",
                        "learning_key": learning_key,
                        "signal": signal.as_dict(),
                    },
                )
                return

            tick = get_tick(symbol_name)
            current_account = account_info()
            current_balance = float(getattr(current_account, "balance", 0.0) or 0.0)
            current_equity = float(getattr(current_account, "equity", 0.0) or current_balance)
            current_risk_base = _account_risk_base(current_account)
            dynamic_base_balance = float(state.get("dynamic_lot_base_balance", session_start_balance) or session_start_balance)
            dynamic_steps = 0
            dynamic_net_profit = 0.0
            requested_volume = float(cfg.signal_fixed_lot)
            channel_lot_override = _channel_lot_override(cfg, signal, channel_username)
            if (
                _is_phoenix_source(signal.chat_id, signal.chat_title)
                and _env_bool("PHOENIX_BALANCE_LOT_SCALING_ENABLED", False)
            ):
                channel_lot_override, phoenix_lot_steps = _phoenix_lot_from_balance(current_balance)
                log.info(
                    f"[LOT] PHOENIX balance scaling: balance={current_balance:.2f} "
                    f"steps={phoenix_lot_steps} lot_per_position={channel_lot_override:.2f}"
                )
            if channel_lot_override is not None:
                requested_volume = channel_lot_override
            elif cfg.signal_lot_mode == "equity_safe":
                requested_volume = _equity_safe_signal_lot(cfg, current_balance, current_equity)
            elif cfg.signal_lot_mode == "funded_safe":
                requested_volume = _funded_safe_signal_lot(cfg, current_balance)
            elif cfg.signal_lot_mode == "profit_dynamic" or cfg.signal_dynamic_lot_enabled:
                requested_volume, dynamic_steps, dynamic_net_profit = _dynamic_lot_from_balance(cfg, dynamic_base_balance, current_balance)
            if signal.asset != "gold":
                broker_minimum = float(getattr(symbol_info(symbol_name), "volume_min", cfg.min_lot) or cfg.min_lot)
                requested_volume = broker_minimum
                volume = normalize_volume(symbol_name, broker_minimum, broker_minimum, broker_minimum)
                log.info(
                    f"[LOT] non-XAU broker minimum: asset={signal.asset} "
                    f"symbol={symbol_name} lot={volume:.2f}"
                )
            else:
                volume = normalize_volume(
                    symbol_name,
                    requested_volume,
                    cfg.min_lot,
                    max(cfg.max_lot, cfg.signal_dynamic_lot_max),
                )
            lot_per_position = (
                channel_lot_override is not None
                or (
                    (cfg.signal_lot_mode == "profit_dynamic" or cfg.signal_dynamic_lot_enabled)
                    and cfg.signal_lot_mode not in {"funded_safe", "equity_safe"}
                    and _env_bool("SIGNAL_DYNAMIC_LOT_PER_POSITION", True)
                )
            )
            strategy = _channel_strategy(signal)
            is_phoenix_signal = _is_phoenix_source(signal.chat_id, signal.chat_title)
            phoenix_calibrated_three_leg = bool(
                is_phoenix_signal and _env_bool("PHOENIX_CALIBRATED_THREE_LEG_ONLY", True)
            )
            is_tfxc_signal = _is_tfxc_premium_source(signal.chat_id, signal.chat_title)
            market_price_now = _market_reference_price(signal.side, tick)
            if is_phoenix_signal and signal.asset == "gold":
                repaired_stop_signal = _repair_phoenix_truncated_stop(signal, market_price_now)
                if repaired_stop_signal != signal:
                    _append_jsonl(
                        events_path,
                        {
                            "type": "signal_levels_repaired",
                            "reason": "xau_truncated_provider_sl",
                            "market_price": market_price_now,
                            "original_signal": signal.as_dict(),
                            "repaired_signal": repaired_stop_signal.as_dict(),
                        },
                    )
                    log.info(
                        f"[REPAIR] PHOENIX reconstructed truncated provider SL: "
                        f"market={market_price_now:.2f} entries={signal.entries} "
                        f"sl={signal.sl}->{repaired_stop_signal.sl}"
                    )
                    signal = repaired_stop_signal
                    strategy = _channel_strategy(signal)
                repaired_signal = _repair_gold_hundred_digit_typo(signal, market_price_now)
                if repaired_signal != signal:
                    _append_jsonl(
                        events_path,
                        {
                            "type": "signal_levels_repaired",
                            "reason": "xau_hundred_digit_typo",
                            "market_price": market_price_now,
                            "original_signal": signal.as_dict(),
                            "repaired_signal": repaired_signal.as_dict(),
                        },
                    )
                    log.info(
                        f"[REPAIR] PHOENIX shifted XAU levels by 100-like typo: "
                        f"market={market_price_now:.2f} entries={signal.entries}->{repaired_signal.entries} "
                        f"sl={signal.sl}->{repaired_signal.sl} tps={signal.tps}->{repaired_signal.tps}"
                    )
                    signal = repaired_signal
                    strategy = _channel_strategy(signal)
                if (
                    _env_bool("PHOENIX_REQUIRE_PROVIDER_SL", True)
                    and not _valid_stop_for_side(signal.side, float(signal.entry or signal.entries[0]), float(signal.sl or 0.0))
                ):
                    log.warning(
                        f"[SKIP] PHOENIX full signal has no trustworthy provider SL after repair: "
                        f"message={int(signal.message_id or 0)} entries={signal.entries} sl={signal.sl}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "phoenix_untrusted_provider_sl",
                            "market_price": market_price_now,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                if not _phoenix_levels_plausible_against_market(signal, market_price_now):
                    nearest_level = min(
                        [abs(float(value) - market_price_now) for value in list(signal.entries) + list(signal.tps) if float(value or 0.0) > 0],
                        default=9999.0,
                    )
                    log.warning(
                        f"[SKIP] PHOENIX implausible levels after repair: "
                        f"market={market_price_now:.2f} nearest_level_distance={nearest_level:.2f} "
                        f"entries={signal.entries} sl={signal.sl} tps={signal.tps}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "phoenix_implausible_levels_after_repair",
                            "market_price": market_price_now,
                            "nearest_level_distance": nearest_level,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
            elif signal.asset == "gold":
                normalized_signal, quote_shift = _normalize_gold_provider_quote_basis(signal, market_price_now)
                if quote_shift:
                    _append_jsonl(
                        events_path,
                        {
                            "type": "signal_levels_normalized",
                            "reason": "provider_quote_basis",
                            "market_price": market_price_now,
                            "quote_shift": quote_shift,
                            "original_signal": signal.as_dict(),
                            "normalized_signal": normalized_signal.as_dict(),
                        },
                    )
                    log.info(
                        f"[NORMALIZE] shifted provider XAU levels by {quote_shift:+.2f}: "
                        f"market={market_price_now:.2f} source={signal.chat_title}"
                    )
                    signal = normalized_signal
                    strategy = _channel_strategy(signal)
                sanity_reason = _ghp_gold_sanity_reason(signal, market_price_now)
                if sanity_reason:
                    log.warning(
                        f"[SKIP] GHP implausible XAU levels: reason={sanity_reason} "
                        f"market={market_price_now:.2f} entries={signal.entries} sl={signal.sl} tps={signal.tps}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": sanity_reason,
                            "market_price": market_price_now,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
            review_execution_mode = "normal"
            if review_agent_enabled:
                review = review_signal(
                    chat_id=signal.chat_id,
                    chat_title=signal.chat_title,
                    asset=signal.asset,
                    side=signal.side,
                    order_kind=signal.order_kind,
                    entries=signal.entries,
                    sl=signal.sl,
                    tps=signal.tps,
                    market_price=market_price_now,
                    raw_text=signal.raw_text,
                    policy=review_policy,
                )
                _append_jsonl(
                    events_path,
                    {
                        "type": "signal_review_agent",
                        "review": review.as_dict(),
                        "market_price": market_price_now,
                        "signal": signal.as_dict(),
                    },
                )
                if review.decision == "reject":
                    log.warning(
                        f"[REVIEW] rejected {signal.uid}: family={review.source_family} "
                        f"reasons={list(review.reasons)}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": f"signal_review_agent:{review.reasons[0]}",
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                log.info(
                    f"[REVIEW] accepted {signal.uid}: family={review.source_family} "
                    f"mode={review.execution_mode} score={review.score}"
                )
                review_execution_mode = str(review.execution_mode or "normal")

            configured_entries = [float(value) for value in signal.entries if float(value or 0.0) > 0]
            provider_retrace_pending = review_execution_mode == "provider_pending"
            phoenix_brain: dict = {}
            phoenix_retrace_pending = False
            if is_phoenix_signal and signal.asset == "gold":
                phoenix_brain = _phoenix_entry_brain(signal, market_price_now, configured_entries)
                phoenix_retrace_pending = _phoenix_wait_for_zone_retrace(phoenix_brain)
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_entry_brain",
                        "market_price": market_price_now,
                        "brain": phoenix_brain,
                        "signal": signal.as_dict(),
                    },
                )
                log.info(
                    f"[PHOENIX-BRAIN] decision={phoenix_brain.get('decision')} "
                    f"market={market_price_now:.2f} zone={phoenix_brain.get('zone_low')}-{phoenix_brain.get('zone_high')} "
                    f"state={phoenix_brain.get('zone_state')} reached_tp={phoenix_brain.get('reached_level')} "
                    f"live_tps={len(phoenix_brain.get('live_tps') or [])}"
                )
                phoenix_always_stage = _phoenix_pending_stage_allowed(
                    signal,
                    phoenix_brain,
                    phoenix_retrace_pending,
                    len(configured_entries),
                    preliminary_range_matched=bool(_phoenix_preliminary_range_covers_signal(signal)),
                )
                phoenix_late_decision = phoenix_brain.get("decision") in {
                    "skip_too_late_after_tp",
                    "skip_not_enough_live_tps",
                }
                if phoenix_late_decision and not _env_bool(
                    "PHOENIX_LATE_SIGNAL_STAGE_PENDING_ENABLED", False
                ):
                    removed_range_pending = _cancel_phoenix_range_pending_for_chat(
                        signal.chat_id,
                        "full_signal_already_after_tp",
                        signal.message_id,
                    )
                    log.info(
                        f"[SKIP] PHOENIX full signal already after TP; removed "
                        f"{removed_range_pending} pre-range pending orders: "
                        f"decision={phoenix_brain.get('decision')} market={market_price_now:.2f} "
                        f"reached_tp={phoenix_brain.get('reached_level')}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": str(phoenix_brain.get("decision")),
                            "market_price": market_price_now,
                            "removed_range_pending": removed_range_pending,
                            "brain": phoenix_brain,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                if (
                    phoenix_late_decision
                    and not phoenix_always_stage
                ):
                    log.info(
                        f"[SKIP] PHOENIX brain rejected late/chased entry: "
                        f"decision={phoenix_brain.get('decision')} market={market_price_now:.2f} "
                        f"reached_tp={phoenix_brain.get('reached_level')} live_tps={phoenix_brain.get('live_tps')}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": str(phoenix_brain.get("decision")),
                            "market_price": market_price_now,
                            "brain": phoenix_brain,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                if phoenix_always_stage and phoenix_brain.get("decision") in {
                    "skip_too_late_after_tp",
                    "skip_not_enough_live_tps",
                }:
                    log.info(
                        f"[PHOENIX-BRAIN] late-entry skip overridden for staged range pending: "
                        f"market={market_price_now:.2f} reached_tp={phoenix_brain.get('reached_level')} "
                        f"zone={phoenix_brain.get('zone_low')}-{phoenix_brain.get('zone_high')}"
                    )
            momentum_chase = False
            momentum_sl = 0.0
            adaptive_near_tp1 = False
            if is_tfxc_signal:
                if signal.asset == "btc":
                    log.info("[SKIP] TFXC BTC disabled after poor live performance; XAU TFXC remains enabled")
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "tfxc_btc_disabled",
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                current_message_key = f"{int(signal.chat_id or 0)}:{int(signal.message_id or 0)}"
                active_tfxc_keys = _active_managed_message_keys_for_channel(signal.chat_id, signal.chat_title) - {current_message_key}
                if TFXC_MAX_ACTIVE_SIGNALS > 0 and len(active_tfxc_keys) >= TFXC_MAX_ACTIVE_SIGNALS:
                    log.info(
                        f"[SKIP] TFXC active-signal cap: active={len(active_tfxc_keys)} "
                        f"max={TFXC_MAX_ACTIVE_SIGNALS}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "tfxc_active_signal_cap",
                            "active_signal_keys": sorted(active_tfxc_keys),
                            "max_active_signals": TFXC_MAX_ACTIVE_SIGNALS,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                recent_tfxc_keys = set()
                if TFXC_SIGNAL_COOLDOWN_MINUTES > 0:
                    recent_tfxc_keys = _recent_managed_message_keys_for_channel(
                        signal.chat_id,
                        signal.chat_title,
                        TFXC_SIGNAL_COOLDOWN_MINUTES,
                    ) - {current_message_key}
                if TFXC_SIGNAL_COOLDOWN_MINUTES > 0 and recent_tfxc_keys:
                    log.info(
                        f"[SKIP] TFXC cooldown: recent={len(recent_tfxc_keys)} "
                        f"minutes={TFXC_SIGNAL_COOLDOWN_MINUTES:.0f}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "tfxc_signal_cooldown",
                            "recent_signal_keys": sorted(recent_tfxc_keys),
                            "cooldown_minutes": TFXC_SIGNAL_COOLDOWN_MINUTES,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                log.info(
                    f"[STRATEGY] TFXC profit guard: one TP1 leg, no stacking, no SL widening, "
                    f"market={market_price_now:.2f} entry={signal.entry}"
                )
            if signal.tps:
                first_tp = float(signal.tps[0])
                remaining_tps = (
                    [float(tp) for tp in signal.tps if float(tp) > market_price_now]
                    if signal.side == "buy"
                    else [float(tp) for tp in signal.tps if float(tp) < market_price_now]
                )
                if (
                    not is_tfxc_signal
                    and not phoenix_retrace_pending
                    and _price_reached_tp(signal.side, market_price_now, first_tp)
                    and remaining_tps
                    and (not is_phoenix_signal or len(remaining_tps) >= 2)
                ):
                    next_tp_distance = abs(float(remaining_tps[0]) - market_price_now)
                    phoenix_continuation = bool(
                        is_phoenix_signal
                        and _phoenix_continuation_market_allowed(
                            signal.side,
                            configured_entries,
                            market_price_now,
                            signal.tps,
                        )
                    )
                    continuation_min_distance = max(
                        _env_float("SIGNAL_CONTINUATION_MIN_NEXT_TP_DISTANCE_USD", 0.5),
                        spread_points * 0.02,
                    )
                    if next_tp_distance >= continuation_min_distance or phoenix_continuation:
                        momentum_chase = True
                        reached_level = _phoenix_reached_tp_level(signal.side, market_price_now, signal.tps)
                        momentum_sl = (
                            float(signal.tps[reached_level - 1])
                            if reached_level > 0
                            else first_tp
                        )
                        log.info(
                            f"[MOMENTUM] TP1 already reached, using one continuation leg: "
                            f"market={market_price_now:.2f} sl={momentum_sl:.2f} next_tp={remaining_tps[0]:.2f} "
                            f"phoenix_continuation={phoenix_continuation}"
                        )
                elif not is_tfxc_signal:
                    live_tps_now = _strict_live_tps_for_entry(signal.side, market_price_now, signal.tps)
                    if len(live_tps_now) >= 2:
                        tp1_distance = abs(float(live_tps_now[0]) - market_price_now)
                        configured_min = max(
                            float(strategy.min_market_tp1_distance or 0.0),
                            _env_float("SIGNAL_MIN_PENDING_TP1_DISTANCE_USD", 0.5),
                        )
                        near_zone = _market_near_entry_zone(
                            configured_entries,
                            market_price_now,
                            max(float(strategy.strict_market_tolerance or 0.0), NEAR_ENTRY_MARKET_TOLERANCE),
                        )
                        close_enough_to_tp1 = tp1_distance <= _env_float("SIGNAL_ADAPTIVE_NEAR_TP1_MAX_DISTANCE_USD", 0.75)
                        if tp1_distance < configured_min and (near_zone or close_enough_to_tp1):
                            momentum_chase = True
                            adaptive_near_tp1 = True
                            momentum_sl = float(signal.sl or 0.0)
                            log.info(
                                f"[ADAPT] price near TP1; using one market leg to live TP2: "
                                f"market={market_price_now:.2f} tp1={live_tps_now[0]:.2f} tp2={live_tps_now[1]:.2f}"
                            )
            use_all_entries = (
                (cfg.signal_entry_mode == "all3" or strategy.force_all_entries or phoenix_retrace_pending)
                and len(configured_entries) > 1
                and not momentum_chase
                and (
                    not phoenix_calibrated_three_leg
                    or phoenix_retrace_pending
                    or (is_phoenix_signal and _env_bool("PHOENIX_STAGED_ZONE_ENTRIES", True))
                )
                and strategy.entry_policy != "nearest_pending_15m"
            )
            phoenix_nine_leg_matrix = (
                is_phoenix_signal
                and _env_bool("PHOENIX_NINE_LEG_MATRIX_ENABLED", False)
                and not momentum_chase
                and len(configured_entries) > 1
            )
            if phoenix_nine_leg_matrix:
                use_all_entries = True
            default_entry = float(signal.entry or 0.0)
            if default_entry <= 0.0:
                default_entry = float(tick.ask if signal.side == "buy" else tick.bid)
            if is_phoenix_signal and configured_entries:
                default_entry = min(configured_entries, key=lambda value: abs(float(value) - float(market_price_now)))
            elif strategy.entry_policy == "nearest_pending_15m" and configured_entries:
                default_entry = min(configured_entries, key=lambda value: abs(float(value) - float(market_price_now)))
            target_plan: list[tuple[int, str, int]] = _split_target_plan_for_strategy(
                strategy,
                is_phoenix_signal=is_phoenix_signal,
                is_tfxc_signal=is_tfxc_signal,
            )
            # A calibrated Phoenix layout still has to adapt when the message
            # arrives at/after TP1. Keeping the original TP1 legs here made the
            # log announce a continuation while the RR guard rejected them.
            if momentum_chase:
                default_entry = market_price_now
                target_plan = [(2, "be", 2)] if adaptive_near_tp1 else [(1, "be", 1)]
                if is_phoenix_signal and str(phoenix_brain.get("decision") or "") == "continuation_after_tp":
                    live_tp_count = len(phoenix_brain.get("live_tps") or [])
                    runner_target = min(3, max(1, live_tp_count))
                    next_tp_distance = abs(float((phoenix_brain.get("live_tps") or [market_price_now])[0]) - market_price_now)
                    if next_tp_distance < max(1.0, spread_points * 0.02):
                        target_plan = [(runner_target, "phoenix_ladder", 6)]
                    else:
                        target_plan = [(1, "be", 1), (runner_target, "phoenix_ladder", 6)]
            elif is_phoenix_signal and phoenix_calibrated_three_leg:
                deep_runner_target = _phoenix_deepest_runner_target(signal.side, market_price_now, signal.tps)
                if phoenix_retrace_pending:
                    target_plan = [(1, "be", 1), (2, "be", 2)]
                    target_plan.append(
                        (deep_runner_target, "phoenix_ladder", deep_runner_target)
                        if deep_runner_target > 0
                        else (3, "phoenix_ladder", 3)
                    )
                    log.info(
                        f"[ENTRY] PHOENIX waiting for zone retrace with limits: market={market_price_now:.2f} "
                        f"zone={phoenix_brain.get('zone_low')}-{phoenix_brain.get('zone_high')}"
                    )
                elif deep_runner_target > 0 and _env_bool("PHOENIX_DEEP_RUNNER_ENABLED", False):
                    target_plan.append((deep_runner_target, "phoenix_ladder", deep_runner_target))
                    log.info(
                        f"[ENTRY] PHOENIX calibrated deep runner added: market={market_price_now:.2f} "
                        f"target=TP{deep_runner_target} protect=phoenix_ladder"
                    )
            if is_phoenix_signal and _env_bool("PHOENIX_COMPLETE_CAPTURE_PROFILE", False):
                mt5_symbol_info = mt5.symbol_info(symbol_name)
                symbol_point = float(getattr(mt5_symbol_info, "point", 0.01) or 0.01) if mt5_symbol_info is not None else 0.01
                capture_target = _tp_one_runner_target_index(
                    signal.side,
                    market_price_now,
                    signal.tps,
                    spread_points * symbol_point,
                )
                if capture_target <= 0:
                    log.info(f"[SKIP] PHOENIX capture profile has no live target: market={market_price_now:.2f}")
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "phoenix_capture_no_live_target",
                            "market_price": market_price_now,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
                momentum_chase = False
                phoenix_retrace_pending = False
                use_all_entries = False
                phoenix_nine_leg_matrix = False
                default_entry = market_price_now
                target_plan = [(capture_target, "none", capture_target)]
                log.info(
                    f"[ENTRY] PHOENIX complete-capture profile: one MARKET leg "
                    f"to live TP{capture_target}, market={market_price_now:.2f}"
                )
            if cfg.signal_lot_mode in {"funded_safe", "equity_safe"} and cfg.signal_funded_skip_below_min:
                min_signal_volume = float(cfg.min_lot) * max(1, len(target_plan))
                if requested_volume + 1e-9 < min_signal_volume:
                    log.info(
                        f"[SKIP] funded lot below executable minimum: requested={requested_volume:.4f} "
                        f"min_signal_volume={min_signal_volume:.2f}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "funded_lot_below_minimum",
                            "requested_volume": requested_volume,
                            "min_signal_volume": min_signal_volume,
                            "signal": signal.as_dict(),
                        },
                    )
                    return
            placement_plan: list[tuple[int, float, int, str, int]] = []
            phoenix_extra_runner_plan_indices: set[int] = set()
            phoenix_profit_plan_indices: set[int] = set()
            runner_target_index = 1
            runner_protect_mode = "be"
            if is_phoenix_signal:
                runner_target_index = max(1, int(_env_float("PHOENIX_MARKET_RUNNER_TARGET_INDEX", 1.0)))
                runner_protect_mode = str(os.getenv("PHOENIX_MARKET_RUNNER_PROTECT_MODE", "be") or "be").strip().lower()
            deep_runner_target = 0
            if phoenix_nine_leg_matrix:
                matrix_entries = list(configured_entries[:3])
                if len(matrix_entries) == 2:
                    low, high = min(matrix_entries), max(matrix_entries)
                    matrix_entries = [low, round((low + high) / 2.0, 3), high]
                while len(matrix_entries) < 3:
                    matrix_entries.append(matrix_entries[-1] if matrix_entries else default_entry)
                matrix_targets = _env_target_tuple("PHOENIX_NINE_LEG_TARGET_INDICES", (1, 2, 5))[:3]
                while len(matrix_targets) < 3:
                    matrix_targets = tuple(list(matrix_targets) + [matrix_targets[-1] if matrix_targets else 1])
                matrix_protect = ("none", "be", "be")
                for entry_index, planned_entry in enumerate(matrix_entries, start=1):
                    for branch_index, target_index in enumerate(matrix_targets, start=1):
                        placement_plan.append(
                            (
                                entry_index,
                                float(planned_entry),
                                int(target_index),
                                matrix_protect[branch_index - 1],
                                int(target_index),
                            )
                        )
                log.info(
                    f"[ENTRY] PHOENIX 3x3 matrix: entries={matrix_entries} "
                    f"targets={list(matrix_targets)} positions={len(placement_plan)}"
                )
            elif use_all_entries:
                phoenix_entry_mapping = str(
                    os.getenv("PHOENIX_ENTRY_TARGET_MAPPING", "legacy") or "legacy"
                ).strip().lower()
                if is_phoenix_signal:
                    entries_to_place = _phoenix_entries_for_target_plan(
                        signal.side,
                        configured_entries,
                        len(target_plan),
                        phoenix_entry_mapping,
                    )
                elif strategy.name == GHP_GOLD_DEEP_BE_STRATEGY.name:
                    provider_midpoint = sum(configured_entries) / max(1, len(configured_entries))
                    entries_to_place = [provider_midpoint] * len(target_plan)
                else:
                    entries_to_place = configured_entries[: len(target_plan)]
                    if len(entries_to_place) < len(target_plan):
                        entries_to_place.extend(
                            [entries_to_place[-1] if entries_to_place else default_entry]
                            * (len(target_plan) - len(entries_to_place))
                        )
                if phoenix_retrace_pending:
                    market_runner_allowed = False
                elif not strategy.allow_market_runner:
                    market_runner_allowed = False
                elif (
                    is_phoenix_signal
                    and bool(_phoenix_preliminary_range_covers_signal(signal))
                    and not _env_bool("PHOENIX_FULL_MARKET_AFTER_PRE_RANGE", False)
                ):
                    market_runner_allowed = False
                    log.info(
                        "[ENTRY] PHOENIX preliminary range already supplied the market leg; "
                        "full signal keeps staged entries without a duplicate market order"
                    )
                elif is_phoenix_signal:
                    market_runner_allowed = _phoenix_market_runner_allowed(signal.side, configured_entries, market_price_now, signal.tps)
                else:
                    market_runner_allowed = _strategy_market_entry_allowed(
                        strategy,
                        signal.side,
                        configured_entries,
                        market_price_now,
                        signal.tps,
                    )
                if market_runner_allowed:
                    placement_plan.append((0, float(market_price_now), runner_target_index, runner_protect_mode, 0))
                    if is_phoenix_signal and _env_bool("PHOENIX_DEEP_RUNNER_ENABLED", False):
                        placement_plan.append((-1, float(market_price_now), 6, "phoenix_ladder", 6))
                        deep_runner_target = _phoenix_deepest_runner_target(signal.side, market_price_now, signal.tps)
                        if deep_runner_target > 0:
                            placement_plan.append(
                                (-2, float(market_price_now), deep_runner_target, "phoenix_ladder", deep_runner_target)
                            )
                    log.info(
                        f"[ENTRY] runner market leg added near entry zone: market={market_price_now:.2f} "
                        f"target=TP{runner_target_index} protect={runner_protect_mode} "
                        f"zone={min(configured_entries):.2f}-{max(configured_entries):.2f} "
                        f"tolerance={(PHOENIX_MARKET_ENTRY_TOLERANCE if is_phoenix_signal else NEAR_ENTRY_MARKET_TOLERANCE):.2f}"
                    )
                    if is_phoenix_signal and deep_runner_target > 0:
                        log.info(
                            f"[ENTRY] PHOENIX runners added: market={market_price_now:.2f} "
                            f"original=TP6 extra=TP{deep_runner_target} protect=phoenix_ladder"
                        )
                planned_targets = target_plan
                skip_entry_indexes = _env_int_set("PHOENIX_SKIP_ENTRY_INDEXES") if is_phoenix_signal else set()
                if (
                    is_phoenix_signal
                    and market_runner_allowed
                    and configured_entries
                    and not _env_bool("PHOENIX_MARKET_RUNNER_EXTRA_LEG", False)
                ):
                    if phoenix_entry_mapping != "legacy":
                        replacement_index = next(
                            (
                                index
                                for index, (target_index, _protect_mode, _plan_index) in enumerate(
                                    planned_targets, start=1
                                )
                                if int(target_index) == int(runner_target_index)
                            ),
                            1,
                        )
                        skip_entry_indexes.add(replacement_index)
                    else:
                        nearest_index = min(
                            range(len(configured_entries)),
                            key=lambda pos: abs(float(configured_entries[pos]) - float(market_price_now)),
                        )
                        skip_entry_indexes.add(nearest_index + 1)
                for entry_index, (planned_entry, (target_index, protect_mode, plan_index)) in enumerate(zip(entries_to_place, planned_targets), start=1):
                    if entry_index in skip_entry_indexes:
                        log.info(
                            f"[ENTRY] PHOENIX skip pending entry index={entry_index} "
                            f"price={float(planned_entry):.2f} target=TP{target_index}"
                        )
                        continue
                    placement_plan.append((entry_index, float(planned_entry), target_index, protect_mode, plan_index))
                placement_plan = _with_nearest_runner_leg(placement_plan, market_price_now, runner_target_index, runner_protect_mode)
            else:
                entries_to_place = [default_entry]
                if not entries_to_place or entries_to_place == [0.0]:
                    entries_to_place = [default_entry]
                for entry_index, planned_entry in enumerate(entries_to_place, start=1):
                    for target_index, protect_mode, plan_index in target_plan:
                        placement_plan.append((entry_index, float(planned_entry), target_index, protect_mode, plan_index))
                if not momentum_chase:
                    placement_plan = _with_nearest_runner_leg(
                        placement_plan,
                        market_price_now,
                        runner_target_index,
                        runner_protect_mode,
                    )
            if is_phoenix_signal and not _env_bool("PHOENIX_FULL_SIGNAL_CORE_ENABLED", True):
                if placement_plan:
                    log.info(
                        f"[ENTRY] PHOENIX core full-signal plan disabled; removed {len(placement_plan)} base legs"
                    )
                placement_plan = []
            if (
                _env_bool("SIGNAL_EXTRA_MARKET_TP1_ENABLED", False)
                and (not is_phoenix_signal or _env_bool("PHOENIX_EXTRA_MARKET_TP1_ENABLED", False))
                and (
                    not is_ghp_source(signal.chat_id, signal.chat_title)
                    or _env_bool("GHP_EXTRA_TP1_RUNNER_ENABLED", False)
                )
                # A Phoenix signal that is already beyond its entry zone must
                # wait for the staged limits; a market runner here defeats the
                # zone-retrace decision and recreates the late-entry losses.
                and not phoenix_retrace_pending
                and not provider_retrace_pending
                and _market_before_live_tp1(signal.side, market_price_now, signal.tps)
            ):
                mt5_symbol_info = mt5.symbol_info(symbol_name)
                symbol_point = float(getattr(mt5_symbol_info, "point", 0.01) or 0.01) if mt5_symbol_info is not None else 0.01
                runner_target_index = _tp_one_runner_target_index(
                    signal.side,
                    market_price_now,
                    signal.tps,
                    spread_points * symbol_point,
                )
                if runner_target_index > 0:
                    runner_count = max(1, int(_env_float("SIGNAL_EXTRA_MARKET_TP1_COUNT", 1.0)))
                    for runner_offset in range(runner_count):
                        placement_plan.append(
                            (-99 - runner_offset, float(market_price_now), runner_target_index, "be", 999)
                        )
                log.info(
                    f"[ENTRY] TP ONE RUNNER added at MARKET for live TP{runner_target_index}: "
                    f"count={max(1, int(_env_float('SIGNAL_EXTRA_MARKET_TP1_COUNT', 1.0)))} "
                    f"market={market_price_now:.2f} spread={spread_points * symbol_point:.2f} "
                    f"source={signal.chat_title}"
                )
            if (
                is_phoenix_signal
                and _env_bool("PHOENIX_EXTRA_TP_RUNNER_ENABLED", False)
                and not phoenix_retrace_pending
            ):
                configured_extra_targets = _env_target_tuple(
                    "PHOENIX_EXTRA_TP_RUNNER_TARGETS",
                    (max(1, int(_env_float("PHOENIX_EXTRA_TP_RUNNER_TARGET_INDEX", 6.0))),),
                )
                configured_extra_protects = tuple(
                    value.strip().lower()
                    for value in str(
                        os.getenv("PHOENIX_EXTRA_TP_RUNNER_PROTECT_MODES", "") or ""
                    ).split(",")
                    if value.strip()
                )
                legacy_extra_protect = str(
                    os.getenv("PHOENIX_EXTRA_TP_RUNNER_PROTECT_MODE", "be_after_tp2")
                    or "be_after_tp2"
                ).strip().lower()
                extra_runner_gate = str(
                    os.getenv("PHOENIX_EXTRA_TP_RUNNER_GATE", "strict_zone") or "strict_zone"
                ).strip().lower()
                extra_runner_min_rr = max(
                    0.0,
                    _env_float("PHOENIX_EXTRA_TP_RUNNER_MIN_RR", 0.0),
                )
                for extra_offset, extra_runner_target in enumerate(configured_extra_targets):
                    live_extra_target = _phoenix_extra_market_runner_target(
                        signal.side,
                        configured_entries,
                        market_price_now,
                        signal.tps,
                        max(1, int(extra_runner_target)),
                        extra_runner_gate,
                    )
                    if live_extra_target <= 0:
                        continue
                    live_extra_tp = float(signal.tps[live_extra_target - 1])
                    planned_rr = _planned_market_reward_risk(
                        market_price_now,
                        signal.sl,
                        live_extra_tp,
                    )
                    if extra_runner_min_rr > 0.0 and planned_rr < extra_runner_min_rr:
                        log.info(
                            f"[ENTRY] PHOENIX extra MARKET runner skipped: "
                            f"target=TP{live_extra_target} rr={planned_rr:.3f} "
                            f"min_rr={extra_runner_min_rr:.3f}"
                        )
                        continue
                    extra_runner_protect = (
                        configured_extra_protects[extra_offset]
                        if extra_offset < len(configured_extra_protects)
                        else legacy_extra_protect
                    )
                    plan_index = 998 - extra_offset
                    placement_plan.append(
                        (
                            -98 + extra_offset,
                            float(market_price_now),
                            live_extra_target,
                            extra_runner_protect,
                            plan_index,
                        )
                    )
                    phoenix_extra_runner_plan_indices.add(plan_index)
                    log.info(
                        f"[ENTRY] PHOENIX extra MARKET runner added: "
                        f"target=TP{live_extra_target} protect={extra_runner_protect} "
                        f"market={market_price_now:.2f} rr={planned_rr:.3f}"
                    )
            elif is_phoenix_signal and phoenix_retrace_pending:
                log.info(
                    "[ENTRY] PHOENIX market runners suppressed while waiting for zone retrace"
                )
            if is_phoenix_signal and _env_bool("PHOENIX_PROFIT_MODULE_ENABLED", False):
                profit_targets = _env_target_tuple("PHOENIX_PROFIT_MODULE_TARGETS", (1, 5, 6))
                profit_protect_modes = tuple(
                    value.strip().lower()
                    for value in str(
                        os.getenv("PHOENIX_PROFIT_MODULE_PROTECT_MODES", "") or ""
                    ).split(",")
                    if value.strip()
                )
                profit_plan = _phoenix_profit_module_plan(
                    configured_entries,
                    profit_targets,
                    profit_protect_modes,
                )
                placement_plan.extend(profit_plan)
                phoenix_profit_plan_indices.update(int(item[4]) for item in profit_plan)
                if profit_plan:
                    log.info(
                        f"[ENTRY] PHOENIX PROFIT module added: entry={profit_plan[0][1]:.2f} "
                        f"targets={list(profit_targets)} legs={len(profit_plan)}"
                    )
            planned_signal_risk_pct = max(
                0.0,
                _env_float("PHOENIX_SIGNAL_RISK_PCT", _env_float("SIGNAL_RISK_PCT", 0.0))
                if is_phoenix_signal
                else _env_float("SIGNAL_RISK_PCT", 0.0),
            )
            planned_leg_risk_pct = _signal_per_leg_risk_pct(is_phoenix_signal)
            risk_managed_legs = planned_signal_risk_pct > 0.0 or planned_leg_risk_pct > 0.0
            if lot_per_position:
                leg_volumes = [float(volume)] * max(1, len(placement_plan))
            elif risk_managed_legs:
                # Risk sizing is applied per leg below. Preserve the complete
                # execution plan instead of trimming it to fit a fallback lot.
                leg_volumes = [float(cfg.min_lot)] * max(1, len(placement_plan))
            else:
                mt5_symbol_info = mt5.symbol_info(symbol_name)
                volume_step = float(getattr(mt5_symbol_info, "volume_step", 0.01) or 0.01)
                volume_min = max(float(cfg.min_lot), float(getattr(mt5_symbol_info, "volume_min", cfg.min_lot) or cfg.min_lot))
                leg_volumes = _split_total_volume(volume, len(placement_plan), volume_step, volume_min)
                if len(leg_volumes) < len(placement_plan):
                    log.info(
                        f"[LOT] trimmed legs from {len(placement_plan)} to {len(leg_volumes)} "
                        f"to preserve total signal volume={volume:.2f}"
                    )
                    placement_plan = placement_plan[: len(leg_volumes)]
            extra_market_tp1_lot = _env_float("SIGNAL_EXTRA_MARKET_TP1_LOT", 0.0)
            extra_market_tp1_multiplier = max(
                0.0,
                _env_float("SIGNAL_EXTRA_MARKET_TP1_LOT_MULTIPLIER", 0.0),
            )
            if extra_market_tp1_lot > 0 or extra_market_tp1_multiplier > 0:
                ordinary_leg_volume = next(
                    (
                        float(leg_volumes[offset])
                        for offset, plan in enumerate(placement_plan)
                        if int(plan[4]) != 999
                    ),
                    float(volume),
                )
                for leg_offset, (_index, _entry, _target, _protect, plan_index) in enumerate(placement_plan):
                    if int(plan_index) == 999:
                        requested_runner_volume = _tp_one_runner_requested_volume(
                            ordinary_leg_volume,
                            extra_market_tp1_lot,
                            extra_market_tp1_multiplier,
                        )
                        leg_volumes[leg_offset] = normalize_volume(
                            symbol_name,
                            requested_runner_volume,
                            cfg.min_lot,
                            max(cfg.max_lot, cfg.signal_dynamic_lot_max),
                        )
                        log.info(
                            f"[LOT] TP ONE RUNNER volume={leg_volumes[leg_offset]:.2f} "
                            f"ordinary_leg={ordinary_leg_volume:.2f} "
                            f"multiplier={extra_market_tp1_multiplier:.2f}"
                        )
            total_signal_volume = round(sum(leg_volumes), 4)

            for leg_offset, (index, planned_entry, target_index, protect_mode, plan_index) in enumerate(placement_plan):
                # Phoenix can move several ticks while a six-leg package is being sent.
                # Re-evaluate every leg against the current quote instead of reusing
                # the tick captured before the placement loop.
                tick = get_tick(symbol_name)
                leg_volume = leg_volumes[leg_offset]
                base_variant_signal = replace(signal, uid=f"{signal.uid}-e{index}", entry=float(planned_entry))
                market_price = _market_reference_price(signal.side, tick)
                has_signal_entry = float(planned_entry or 0.0) > 0.0
                entry_price = float(planned_entry) if (use_all_entries or signal.order_kind in {"limit", "stop"}) else market_price
                order_kind = signal.order_kind
                if int(plan_index) == 999:
                    entry_price = market_price
                    order_kind = "market"
                    base_variant_signal = replace(
                        base_variant_signal,
                        entry=float(entry_price),
                        order_kind=order_kind,
                        order_type=_order_type(signal.side, order_kind),
                    )
                elif provider_retrace_pending:
                    entry_price = float(planned_entry)
                    order_kind = _pending_kind_for_entry(signal.side, entry_price, tick)
                    base_variant_signal = replace(
                        base_variant_signal,
                        entry=entry_price,
                        order_kind=order_kind,
                        order_type=_order_type(signal.side, order_kind),
                    )
                elif int(plan_index) in phoenix_profit_plan_indices:
                    # Deep Phoenix runners must wait at the configured zone
                    # midpoint. They are an additional retracement package,
                    # never a late market chase inherited from the provider post.
                    entry_price = float(planned_entry)
                    order_kind = _pending_kind_for_entry(signal.side, entry_price, tick)
                    base_variant_signal = replace(
                        base_variant_signal,
                        entry=entry_price,
                        order_kind=order_kind,
                        order_type=_order_type(signal.side, order_kind),
                    )
                elif strategy.entry_policy == "nearest_pending_15m":
                    entry_price = float(planned_entry)
                    order_kind = "limit"
                    base_variant_signal = replace(base_variant_signal, entry=float(entry_price), order_kind=order_kind, order_type=_order_type(signal.side, order_kind))
                elif index <= 0 and use_all_entries:
                    entry_price = market_price
                    order_kind = "market"
                    base_variant_signal = replace(base_variant_signal, entry=float(entry_price), order_kind=order_kind, order_type=_order_type(signal.side, order_kind))
                elif momentum_chase:
                    entry_price = market_price
                    order_kind = "market"
                    base_variant_signal = replace(base_variant_signal, order_kind=order_kind, order_type=_order_type(signal.side, order_kind))
                elif use_all_entries and order_kind == "market":
                    if is_phoenix_signal or is_ghp_source(signal.chat_id, signal.chat_title):
                        order_kind = _pending_kind_for_entry(signal.side, float(planned_entry), tick)
                    else:
                        order_kind = "limit"
                    base_variant_signal = replace(base_variant_signal, order_kind=order_kind, order_type=_order_type(signal.side, order_kind))
                elif order_kind == "market" and has_signal_entry:
                    entry_gap = abs(float(planned_entry) - market_price)
                    entry_gap_limit = max(NEAR_ENTRY_MARKET_TOLERANCE, _market_entry_gap_limit(symbol_name, spread_points, cfg.signal_sl_min_points))
                    market_before_tp1_allowed = (
                        _strategy_uses_market_before_tp1(strategy)
                        and _market_before_live_tp1(signal.side, market_price, signal.tps)
                        and _market_order_allowed_for_strategy(
                            strategy,
                            signal.side,
                            float(planned_entry),
                            market_price,
                            signal.tps,
                        )
                    )
                    strategy_market_allowed = market_before_tp1_allowed or (
                        bool(strategy.allow_market_runner)
                        and _market_order_allowed_for_strategy(
                            strategy,
                            signal.side,
                            float(planned_entry),
                            market_price,
                            signal.tps,
                        )
                    )
                    if ((entry_gap > entry_gap_limit) and not market_before_tp1_allowed) or not strategy_market_allowed:
                        entry_price = float(planned_entry)
                        order_kind = _pending_kind_for_entry(signal.side, entry_price, tick)
                        base_variant_signal = replace(base_variant_signal, order_kind=order_kind, order_type=_order_type(signal.side, order_kind))
                        log.info(
                            f"[ENTRY] converted market to {signal.side.upper()} {order_kind.upper()}: "
                            f"signal_entry={entry_price} market={market_price} gap={entry_gap:.2f} "
                            f"limit={entry_gap_limit:.2f} strategy_market_allowed={strategy_market_allowed}"
                        )

                if order_kind == "limit":
                    pending_reason = _invalid_pending_reason(signal.side, order_kind, entry_price, tick)
                    entry_gap = abs(float(entry_price) - market_price)
                    entry_gap_limit = _market_entry_gap_limit(symbol_name, spread_points, cfg.signal_sl_min_points)
                    tp1_reached = bool(signal.tps and _price_reached_tp(signal.side, market_price, float(signal.tps[0])))
                    phoenix_price_in_zone = (
                        is_phoenix_signal
                        and pending_reason in {"buy_limit_entry_not_below_market", "sell_limit_entry_not_above_market"}
                        and not tp1_reached
                        and bool(_strict_live_tps_for_entry(signal.side, market_price, signal.tps))
                    )
                    strategy_price_in_zone = (
                        not is_phoenix_signal
                        and pending_reason in {"buy_limit_entry_not_below_market", "sell_limit_entry_not_above_market"}
                        and not tp1_reached
                        and _strategy_market_entry_allowed(strategy, signal.side, [entry_price], market_price, signal.tps)
                    )
                    if (
                        pending_reason in {"buy_limit_entry_not_below_market", "sell_limit_entry_not_above_market"}
                        and (entry_gap <= entry_gap_limit or phoenix_price_in_zone or strategy_price_in_zone)
                        and (not is_phoenix_signal or _phoenix_market_runner_allowed(signal.side, configured_entries, market_price, signal.tps))
                        and (is_phoenix_signal or _market_order_allowed_for_strategy(strategy, signal.side, entry_price, market_price, signal.tps))
                        and not tp1_reached
                        and int(plan_index) not in phoenix_profit_plan_indices
                    ):
                        log.info(
                            f"[ENTRY] touched {signal.side.upper()} LIMIT converted to MARKET: "
                            f"entry={entry_price} market={market_price} gap={entry_gap:.2f} limit={entry_gap_limit:.2f}"
                        )
                        entry_price = market_price
                        order_kind = "market"
                        if is_phoenix_signal:
                            repaired_tps = _repair_tps_for_entry(signal.side, entry_price, signal.tps, max(target_index, 4))
                            if repaired_tps != signal.tps:
                                signal = replace(signal, tp=float(repaired_tps[0]), tps=repaired_tps)
                                log.info(
                                    f"[STRATEGY] PHOENIX repaired live TPs against market: "
                                    f"entry={entry_price:.2f} tps={repaired_tps}"
                                )
                        base_variant_signal = replace(
                            base_variant_signal,
                            entry=float(entry_price),
                            tp=float(signal.tps[0]) if signal.tps else base_variant_signal.tp,
                            tps=[float(value) for value in signal.tps],
                            order_kind=order_kind,
                            order_type=_order_type(signal.side, order_kind),
                        )

                phoenix_range_derived_sl = False
                if momentum_chase and _valid_stop_for_side(signal.side, entry_price, momentum_sl):
                    sl = _cap_stop_distance(symbol_name, signal.side, entry_price, momentum_sl)
                    if is_phoenix_signal and not _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0)):
                        sl = _phoenix_range_stop_loss(signal, symbol_name, entry_price, sl)
                        phoenix_range_derived_sl = True
                elif (
                    is_phoenix_signal
                    and _env_bool("PHOENIX_PRESERVE_SIGNAL_SL", True)
                    and _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0))
                ):
                    sl = float(signal.sl)
                elif (
                    is_ghp_source(signal.chat_id, signal.chat_title)
                    and _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0))
                ):
                    # GHP publishes unusually wide provider stops. Replacing
                    # them with the generic six-dollar optimized stop caused
                    # avoidable stop-outs before the advertised move.
                    sl = float(signal.sl)
                elif is_phoenix_signal and int(target_index) == 6 and _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0)):
                    sl = float(signal.sl)
                else:
                    sl = _optimized_stop_loss(
                        symbol_name,
                        signal.side,
                        entry_price,
                        float(signal.sl or 0.0),
                        spread_points,
                        cfg.signal_sl_atr_mult,
                        cfg.signal_sl_min_points,
                    )
                    if is_phoenix_signal and not _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0)):
                        derived_sl = _phoenix_range_stop_loss(signal, symbol_name, entry_price, sl)
                        if derived_sl != sl:
                            log.info(
                                f"[PHOENIX-MOMENTUM] derived range SL: "
                                f"entry={entry_price:.2f} sl={sl:.2f}->{derived_sl:.2f} "
                                f"range={min(signal.entries):.2f}-{max(signal.entries):.2f}"
                            )
                            sl = derived_sl
                            phoenix_range_derived_sl = True
                if order_kind == "market":
                    safe_sl = _minimum_safe_stop(
                        symbol_name,
                        signal.side,
                        entry_price,
                        sl,
                        spread_points,
                        cfg.signal_sl_min_points,
                    )
                    if safe_sl != sl:
                        log.info(
                            f"[LEVELS] adjusted market SL to broker-safe distance: "
                            f"entry={entry_price:.2f} sl={sl:.2f}->{safe_sl:.2f}"
                        )
                        sl = safe_sl
                if "gold_btc_xauusd_zone_test" in str(strategy.name or ""):
                    sl = _zone_stop_loss_for_signal(
                        signal,
                        entry_price,
                        sl,
                        _env_float("GOLD_BTC_XAUUSD_ZONE_SL_BUFFER_USD", 8.0),
                    )
                if phoenix_retrace_pending:
                    if _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0)):
                        sl = float(signal.sl)
                    else:
                        sl = _phoenix_range_stop_loss(signal, symbol_name, entry_price, sl)
                        phoenix_range_derived_sl = True
                strategy_stop_cap = float(strategy.max_stop_distance or 0.0)
                if phoenix_calibrated_three_leg and strategy_stop_cap <= 0.0:
                    strategy_stop_cap = _env_float("PHOENIX_MAX_STOP_DISTANCE_USD", 6.0)
                preserve_phoenix_provider_sl = (
                    is_phoenix_signal
                    and _env_bool("PHOENIX_PRESERVE_SIGNAL_SL", True)
                    and _valid_stop_for_side(signal.side, entry_price, float(signal.sl or 0.0))
                )
                phoenix_provider_sl = float(signal.sl or 0.0)
                if preserve_phoenix_provider_sl:
                    phoenix_provider_sl = _phoenix_provider_stop_with_cap(
                        signal.side,
                        entry_price,
                        phoenix_provider_sl,
                        max(0.0, _env_float("PHOENIX_PROVIDER_SL_MAX_DISTANCE_USD", 12.0)),
                    )
                if (
                    strategy_stop_cap > 0.0
                    and not phoenix_retrace_pending
                    and not preserve_phoenix_provider_sl
                    and not phoenix_range_derived_sl
                ):
                    sl = _cap_stop_distance_value(
                        symbol_name,
                        signal.side,
                        entry_price,
                        sl,
                        strategy_stop_cap,
                    )
                elif preserve_phoenix_provider_sl:
                    sl = phoenix_provider_sl
                if int(plan_index) in phoenix_profit_plan_indices:
                    sl = _phoenix_provider_stop_with_cap(
                        signal.side,
                        entry_price,
                        float(signal.sl or sl),
                        max(0.1, _env_float("PHOENIX_PROFIT_MODULE_SL_CAP_USD", 8.0)),
                    )
                if is_phoenix_signal and int(plan_index) == 999:
                    sl = _phoenix_provider_stop_with_cap(
                        signal.side,
                        entry_price,
                        float(signal.sl or sl),
                        max(0.1, _env_float("PHOENIX_TP1_RUNNER_SL_CAP_USD", 12.0)),
                    )

                execution_block_reason = _review_execution_order_block_reason(
                    review_execution_mode,
                    order_kind,
                )
                if execution_block_reason:
                    log.warning(
                        f"[SKIP] review requires provider pending; blocked MARKET: "
                        f"entry={entry_price:.2f} market={market_price:.2f} source={signal.chat_title}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": execution_block_reason,
                            "entry_price": entry_price,
                            "market_price": market_price,
                            "review_execution_mode": review_execution_mode,
                            "signal": base_variant_signal.as_dict(),
                        },
                    )
                    continue

                if order_kind in {"limit", "stop"}:
                    pending_reason = _invalid_pending_reason(signal.side, order_kind, entry_price, tick)
                    if pending_reason:
                        log.info(f"[SKIP] {pending_reason}: entry={entry_price} bid={tick.bid} ask={tick.ask}")
                        _append_jsonl(
                            events_path,
                            {
                                "type": "skip",
                                "reason": pending_reason,
                                "entry_price": entry_price,
                                "bid": float(tick.bid),
                                "ask": float(tick.ask),
                                "signal": base_variant_signal.as_dict(),
                            },
                        )
                        continue

                variant_signal = replace(base_variant_signal, uid=f"{base_variant_signal.uid}-t{plan_index}")
                effective_strategy = replace(strategy, target_index=target_index, protect_mode=protect_mode)
                if is_phoenix_signal:
                    repaired_tps = _repair_tps_for_entry(signal.side, entry_price, signal.tps, max(target_index, 4))
                    if repaired_tps != signal.tps:
                        signal = replace(signal, tp=float(repaired_tps[0]), tps=repaired_tps)
                        variant_signal = replace(variant_signal, tp=float(repaired_tps[0]), tps=repaired_tps)
                        log.info(
                            f"[STRATEGY] PHOENIX repaired TP ladder for entry={entry_price:.2f}: tps={repaired_tps}"
                        )
                try:
                    tp1, execution_tp, live_tp_index, live_tps = _select_live_tps(signal.side, entry_price, signal.tps, target_index)
                except ValueError as exc:
                    log.info(f"[SKIP] {exc}: entry={entry_price} tps={signal.tps}")
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": str(exc),
                            "entry_price": entry_price,
                            "signal": variant_signal.as_dict(),
                        },
                    )
                    continue

                if order_kind in {"limit", "stop"} and not phoenix_retrace_pending and not provider_retrace_pending:
                    current_to_tp1 = abs(float(tp1) - float(market_price))
                    min_pending_tp1_distance = max(
                        float(effective_strategy.min_market_tp1_distance or 0.0),
                        _env_float("SIGNAL_MIN_PENDING_TP1_DISTANCE_USD", 1.5),
                    )
                    market_already_at_tp1 = _price_reached_tp(signal.side, market_price, tp1)
                    if market_already_at_tp1 or current_to_tp1 < min_pending_tp1_distance:
                        reason = "pending_after_tp1" if market_already_at_tp1 else "pending_too_close_to_tp1"
                        log.info(
                            f"[SKIP] {reason}: entry={entry_price} market={market_price} tp1={tp1} "
                            f"distance={current_to_tp1:.2f} min={min_pending_tp1_distance:.2f}"
                        )
                        _append_jsonl(
                            events_path,
                            {
                                "type": "skip",
                                "reason": reason,
                                "entry_price": entry_price,
                                "market_price": market_price,
                                "tp1": tp1,
                                "distance_to_tp1": round(current_to_tp1, 2),
                                "min_distance_to_tp1": round(min_pending_tp1_distance, 2),
                                "signal": variant_signal.as_dict(),
                            },
                        )
                        continue

                rr_reference_tp = execution_tp if (momentum_chase or int(target_index) > 1) else tp1
                if order_kind == "market" and not _market_tp1_reward_ok(signal.side, entry_price, sl, rr_reference_tp):
                    reward = abs(float(rr_reference_tp) - float(entry_price))
                    risk = abs(float(entry_price) - float(sl))
                    rr = reward / risk if risk > 0 else 0.0
                    log.info(
                        f"[SKIP] weak market target/RR: entry={entry_price} sl={sl} target={rr_reference_tp} "
                        f"reward={reward:.2f} risk={risk:.2f} rr={rr:.2f} min={MIN_MARKET_TP1_RR:.2f}"
                    )
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": "weak_market_target_rr",
                            "entry_price": entry_price,
                            "sl": sl,
                            "tp1": tp1,
                            "target": rr_reference_tp,
                            "reward": reward,
                            "risk": risk,
                            "rr": rr,
                            "min_rr": MIN_MARKET_TP1_RR,
                            "signal": variant_signal.as_dict(),
                        },
                    )
                    continue

                if order_kind == "market":
                    reward_reference_tp = execution_tp if (momentum_chase or int(target_index) > 1) else tp1
                    spread_ok, reward, net_reward, required_reward = _market_tp1_spread_reward_ok(
                        symbol_name,
                        entry_price,
                        reward_reference_tp,
                        spread_points,
                    )
                    if not spread_ok:
                        log.info(
                            f"[SKIP] weak market target after spread: entry={entry_price} target={reward_reference_tp} "
                            f"reward={reward:.2f} net={net_reward:.2f} required_reward={required_reward:.2f}"
                        )
                        _append_jsonl(
                            events_path,
                            {
                                "type": "skip",
                                "reason": "weak_market_tp1_after_spread",
                                "entry_price": entry_price,
                                "tp1": tp1,
                                "reward": reward,
                                "net_reward": net_reward,
                                "required_reward": required_reward,
                                "spread_points": spread_points,
                                "signal": variant_signal.as_dict(),
                            },
                        )
                        continue

                levels_reason = _invalid_levels_reason(signal.side, entry_price, sl, tp1)
                if levels_reason:
                    log.info(f"[SKIP] {levels_reason}: entry={entry_price} sl={sl} tp1={tp1}")
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": levels_reason,
                            "entry_price": entry_price,
                            "sl": sl,
                            "tp": tp1,
                            "signal": variant_signal.as_dict(),
                        },
                    )
                    continue

                execution_levels_reason = _invalid_levels_reason(signal.side, entry_price, sl, execution_tp)
                if execution_levels_reason:
                    log.info(f"[SKIP] {execution_levels_reason}: entry={entry_price} sl={sl} execution_tp={execution_tp}")
                    _append_jsonl(
                        events_path,
                        {
                            "type": "skip",
                            "reason": execution_levels_reason,
                            "entry_price": entry_price,
                            "sl": sl,
                            "tp": execution_tp,
                            "signal": variant_signal.as_dict(),
                        },
                    )
                    continue

                signal_risk_pct = max(
                    0.0,
                    _env_float("PHOENIX_SIGNAL_RISK_PCT", _env_float("SIGNAL_RISK_PCT", 0.0))
                    if is_phoenix_signal
                    else _env_float("SIGNAL_RISK_PCT", 0.0),
                )
                signal_leg_risk_pct = _signal_per_leg_risk_pct(is_phoenix_signal)
                if (signal_risk_pct > 0.0 or signal_leg_risk_pct > 0.0) and int(plan_index) != 999:
                    leg_count_for_risk = max(1, len(placement_plan))
                    risk_per_leg = _risk_usd_per_leg(
                        current_risk_base,
                        signal_risk_pct,
                        leg_count_for_risk,
                        signal_leg_risk_pct,
                    )
                    if int(plan_index) in phoenix_extra_runner_plan_indices:
                        risk_per_leg *= max(
                            0.0,
                            _env_float("PHOENIX_EXTRA_TP_RUNNER_RISK_MULTIPLIER", 1.0),
                        )
                    if (
                        is_phoenix_signal
                        and int(target_index) == 6
                        and int(plan_index) not in phoenix_profit_plan_indices
                    ):
                        risk_per_leg *= max(
                            0.0,
                            _env_float("PHOENIX_TP6_RISK_MULTIPLIER", 1.0),
                        )
                    loss_per_lot = calc_loss_per_lot(symbol_name, signal.side, entry_price, sl)
                    if loss_per_lot > 0.0:
                        minimum_leg_lot = (
                            max(cfg.min_lot, _env_float("PHOENIX_MIN_LEG_LOT", cfg.min_lot))
                            if is_phoenix_signal
                            else cfg.min_lot
                        )
                        leg_volume = normalize_volume(
                            symbol_name,
                            max(minimum_leg_lot, risk_per_leg / loss_per_lot),
                            cfg.min_lot,
                            max(cfg.max_lot, cfg.signal_dynamic_lot_max),
                        )
                        log.info(
                            f"[RISK LOT] signal={signal_risk_pct:.2f}% per_leg={signal_leg_risk_pct:.2f}% "
                            f"legs={leg_count_for_risk} "
                            f"risk_per_leg={risk_per_leg:.2f} loss_per_lot={loss_per_lot:.2f} volume={leg_volume:.2f}"
                        )
                elif (
                    (signal_risk_pct > 0.0 or signal_leg_risk_pct > 0.0)
                    and int(plan_index) == 999
                    and extra_market_tp1_multiplier > 0.0
                ):
                    leg_count_for_risk = max(1, len(placement_plan))
                    risk_per_leg = _risk_usd_per_leg(
                        current_risk_base,
                        signal_risk_pct,
                        leg_count_for_risk,
                        signal_leg_risk_pct,
                    )
                    loss_per_lot = calc_loss_per_lot(symbol_name, signal.side, entry_price, sl)
                    if loss_per_lot > 0.0:
                        minimum_leg_lot = (
                            max(cfg.min_lot, _env_float("PHOENIX_MIN_LEG_LOT", cfg.min_lot))
                            if is_phoenix_signal
                            else cfg.min_lot
                        )
                        ordinary_equivalent = normalize_volume(
                            symbol_name,
                            max(minimum_leg_lot, risk_per_leg / loss_per_lot),
                            cfg.min_lot,
                            max(cfg.max_lot, cfg.signal_dynamic_lot_max),
                        )
                        leg_volume = normalize_volume(
                            symbol_name,
                            ordinary_equivalent * extra_market_tp1_multiplier,
                            cfg.min_lot,
                            max(cfg.max_lot, cfg.signal_dynamic_lot_max),
                        )
                        log.info(
                            f"[RISK LOT] TP ONE RUNNER ordinary={ordinary_equivalent:.2f} "
                            f"multiplier={extra_market_tp1_multiplier:.2f} volume={leg_volume:.2f}"
                        )

                comment = _order_comment(variant_signal)
                if int(plan_index) == 999:
                    comment = f"{comment[:20]}:TP1RUN"[:31]
                elif int(plan_index) in phoenix_extra_runner_plan_indices:
                    comment = f"PHOENIX:EXTRA TP{int(target_index)}"[:31]
                elif int(plan_index) in phoenix_profit_plan_indices:
                    comment = f"PHOENIX:PROFIT TP{int(target_index)}"[:31]
                broker_sl = 0.0 if _delay_broker_levels_for_min_hold() else sl
                broker_tp = 0.0 if _delay_broker_levels_for_min_hold() else execution_tp
                if order_kind in {"limit", "stop"}:
                    result = send_pending_order(
                        symbol=symbol_name,
                        side=signal.side,
                        order_kind=order_kind,
                        volume=leg_volume,
                        price=entry_price,
                        sl=broker_sl,
                        tp=broker_tp,
                        deviation=cfg.deviation,
                        magic=cfg.magic,
                        comment=comment,
                    )
                else:
                    result = send_market_order(
                        symbol=symbol_name,
                        side=signal.side,
                        volume=leg_volume,
                        sl=broker_sl,
                        tp=broker_tp,
                        deviation=cfg.deviation,
                        magic=cfg.magic,
                        comment=comment,
                    )
                retcode = getattr(result, "retcode", None)
                if retcode == 10015 and order_kind in {"limit", "stop"}:
                    retry_tick = get_tick(symbol_name)
                    retry_market = _market_reference_price(signal.side, retry_tick)
                    retry_kind = _pending_kind_for_entry(signal.side, float(planned_entry), retry_tick)
                    retry_as_market = bool(
                        is_phoenix_signal
                        and _market_before_live_tp1(signal.side, retry_market, signal.tps)
                        and _phoenix_market_runner_allowed(
                            signal.side,
                            configured_entries,
                            retry_market,
                            signal.tps,
                        )
                    )
                    initial_retcode = retcode
                    if retry_as_market:
                        entry_price = retry_market
                        order_kind = "market"
                        sl = _minimum_safe_stop(
                            symbol_name,
                            signal.side,
                            entry_price,
                            sl,
                            spread_points,
                            cfg.signal_sl_min_points,
                        )
                        broker_sl = 0.0 if _delay_broker_levels_for_min_hold() else sl
                        broker_tp = 0.0 if _delay_broker_levels_for_min_hold() else execution_tp
                        variant_signal = replace(
                            variant_signal,
                            entry=float(entry_price),
                            order_kind="market",
                            order_type=_order_type(signal.side, "market"),
                        )
                        result = send_market_order(
                            symbol=symbol_name,
                            side=signal.side,
                            volume=leg_volume,
                            sl=broker_sl,
                            tp=broker_tp,
                            deviation=cfg.deviation,
                            magic=cfg.magic,
                            comment=comment,
                        )
                    else:
                        order_kind = retry_kind
                        variant_signal = replace(
                            variant_signal,
                            order_kind=retry_kind,
                            order_type=_order_type(signal.side, retry_kind),
                        )
                        result = send_pending_order(
                            symbol=symbol_name,
                            side=signal.side,
                            order_kind=retry_kind,
                            volume=leg_volume,
                            price=entry_price,
                            sl=broker_sl,
                            tp=broker_tp,
                            deviation=cfg.deviation,
                            magic=cfg.magic,
                            comment=comment,
                        )
                    retcode = getattr(result, "retcode", None)
                    _append_jsonl(
                        events_path,
                        {
                            "type": "order_retry_after_invalid_price",
                            "initial_retcode": initial_retcode,
                            "retry_retcode": retcode,
                            "retry_as_market": retry_as_market,
                            "retry_order_kind": order_kind,
                            "entry_price": entry_price,
                            "market_price": retry_market,
                            "signal": variant_signal.as_dict(),
                        },
                    )
                    log.info(
                        f"[RETRY] invalid pending repriced as {order_kind.upper()}: "
                        f"entry={entry_price:.2f} market={retry_market:.2f} retcode={retcode}"
                    )
                deal_ticket = int(getattr(result, "deal", 0) or 0)
                order_ticket = int(getattr(result, "order", 0) or 0)
                order_success = retcode in {10008, 10009}
                payload = {
                    "type": "order_attempt",
                    "symbol": symbol_name,
                    "asset": signal.asset,
                    "retcode": retcode,
                    "deal_ticket": deal_ticket,
                    "order_ticket": order_ticket,
                    "volume": leg_volume,
                    "requested_volume": requested_volume,
                    "total_signal_volume": total_signal_volume,
                    "leg_volume": leg_volume,
                    "signal_risk_pct": signal_risk_pct,
                    "lot_per_position": lot_per_position,
                    "channel_lot_override": channel_lot_override,
                    "dynamic_lot_enabled": cfg.signal_dynamic_lot_enabled,
                    "dynamic_lot_base_balance": dynamic_base_balance,
                    "dynamic_lot_current_balance": current_balance,
                    "dynamic_lot_current_equity": current_equity,
                    "equity_safe_compound_base": _equity_safe_compound_base(current_balance, current_equity),
                    "dynamic_lot_net_profit": dynamic_net_profit,
                    "dynamic_lot_steps": dynamic_steps,
                    "dynamic_lot_step_usd": cfg.signal_dynamic_lot_step_usd,
                    "spread_points": spread_points,
                    "entry_price": entry_price,
                    "sl": sl,
                    "tp": execution_tp,
                    "broker_sl": broker_sl,
                    "broker_tp": broker_tp,
                    "tp1": tp1,
                    "tp_target_index": live_tp_index,
                    "live_tps": live_tps,
                    "strategy": effective_strategy.name,
                    "protect_mode": effective_strategy.protect_mode,
                    "atr_mult": effective_strategy.atr_mult,
                    "entry_mode": "all3" if use_all_entries else "split3",
                    "entry_index": index,
                    "entries_count": len(configured_entries),
                    "target_plan_index": plan_index,
                    "comment": comment,
                    "execution_module": "phoenix_profit" if int(plan_index) in phoenix_profit_plan_indices else "",
                    "pending_expiry_override_minutes": (
                        _env_float("PHOENIX_PROFIT_MODULE_PENDING_MINUTES", 60.0)
                        if int(plan_index) in phoenix_profit_plan_indices
                        else 0.0
                    ),
                    "ignore_channel_pending_cancel": bool(int(plan_index) in phoenix_profit_plan_indices),
                    "signal": variant_signal.as_dict(),
                }
                _append_jsonl(orders_path, payload)
                _append_jsonl(events_path, payload)
                if order_success:
                    any_success = True
                    processed.add(signal.uid)
                    processed.add(variant_signal.uid)
                    _remember_signal_signature(signal)
                    state["last_signal"] = variant_signal.as_dict()
                    state["last_order"] = payload
                    _save_runtime_state()
                    _remember_managed_signal(variant_signal, payload, entry_price, sl, execution_tp, effective_strategy)
                    log.info(
                        f"[LIVE] placed {signal.side.upper()} {order_kind.upper()} on {symbol_name} "
                        f"strategy={effective_strategy.name} entry={entry_price} sl={sl} tp1={tp1} "
                        f"tp={execution_tp} vol={leg_volume}/{total_signal_volume} retcode={retcode}"
                    )
                else:
                    log.warning(
                        f"[LIVE] order rejected for {variant_signal.uid} retcode={retcode} "
                        f"last_error={mt5.last_error() if mt5 else 'mt5-unavailable'} "
                        f"volume={leg_volume:.2f} sl={broker_sl} tp={broker_tp}"
                    )
            if not any_success:
                log.info(f"[SKIP] no entries placed for {signal.uid}")
        except Exception as exc:
            log.warning(f"[LIVE] order failed for {signal.uid}: {type(exc).__name__}: {exc}")
            retryable = _is_retryable_market_data_error(exc) and not any_success
            _append_jsonl(
                events_path,
                {
                    "type": "error",
                    "signal": signal.as_dict(),
                    "error": str(exc),
                    "retryable": retryable,
                },
            )
            if retryable:
                _append_jsonl(
                    events_path,
                    {
                        "type": "retry_scheduled",
                        "reason": "market_data_unavailable",
                        "signal": signal.as_dict(),
                    },
                )
                log.warning(
                    f"[RETRY] market data unavailable for {signal.uid}; "
                    "watchdog will retry without marking the message as processed"
                )
                raise

    def _cancel_pending_for_chat(
        chat_id: int | None,
        chat_title: str,
        text: str,
        message_id: int,
        reply_to_message_id: int = 0,
    ) -> int:
        signals = managed_state.setdefault("signals", {})
        removed = 0
        normalized_title = str(chat_title or "").strip().lower()
        active_order_tickets = {
            int(getattr(order, "ticket", 0) or 0)
            for managed_symbol in _active_symbols()
            for order in orders_by_magic(managed_symbol, cfg.magic)
        }
        active_rows = [
            managed
            for managed in signals.values()
            if int(managed.get("order_ticket", 0) or 0) in active_order_tickets
        ]
        target_message_ids = _channel_update_target_message_ids(
            active_rows,
            chat_id,
            chat_title,
            message_id,
            reply_to_message_id,
        )
        if not target_message_ids:
            log.info(
                f"[CANCEL] ignored unscoped channel update message={message_id} "
                f"reply_to={reply_to_message_id}"
            )
            return 0
        for managed_symbol in _active_symbols():
            for order in orders_by_magic(managed_symbol, cfg.magic):
                ticket = int(getattr(order, "ticket", 0) or 0)
                managed_match = None
                for signal_id, managed in list(signals.items()):
                    try:
                        if int(managed.get("order_ticket", 0) or 0) != ticket:
                            continue
                    except Exception:
                        continue
                    managed_chat_id = int(managed.get("chat_id", 0) or 0)
                    managed_title = str(managed.get("chat_title", "") or "").strip().lower()
                    same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
                    same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
                    if (same_chat or same_title) and _managed_source_message_ids(managed) & target_message_ids:
                        managed_match = (signal_id, managed)
                        break
                if managed_match is None:
                    continue
                signal_id, managed = managed_match
                if bool(managed.get("ignore_channel_pending_cancel", False)):
                    continue
                current_price = 0.0
                try:
                    tick = get_tick(managed_symbol)
                    managed_side = str(managed.get("side", "") or "").lower()
                    current_price = float(tick.ask if managed_side == "buy" else tick.bid)
                except Exception as exc:
                    log.warning(
                        f"[CANCEL-AGENT] market context unavailable for {signal_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                cancel_review = review_provider_pending_update(
                    text=text,
                    scoped=True,
                    side=str(managed.get("side", "") or "").lower(),
                    asset=str(managed.get("asset", "") or "").lower(),
                    entry=float(managed.get("entry", 0.0) or 0.0),
                    tp1=float(managed.get("tp1", 0.0) or 0.0),
                    current_price=current_price,
                    created_utc=str(managed.get("created_utc", "") or ""),
                )
                _append_jsonl(
                    events_path,
                    {
                        "type": "provider_pending_update_agent",
                        "signal_id": signal_id,
                        "order_ticket": ticket,
                        "symbol": managed_symbol,
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "message_id": message_id,
                        "current_price": current_price,
                        "review": cancel_review.as_dict(),
                        "text": text,
                    },
                )
                if cancel_review.decision != "cancel":
                    log.warning(
                        f"[CANCEL-AGENT] kept pending {ticket} for {signal_id}: "
                        f"decision={cancel_review.decision} reasons={','.join(cancel_review.reasons)}"
                    )
                    continue
                result = remove_order(order, magic=cfg.magic)
                retcode = getattr(result, "retcode", None)
                _append_jsonl(
                    events_path,
                    {
                        "type": "channel_cancel_remove_attempt",
                        "signal_id": signal_id,
                        "order_ticket": ticket,
                        "symbol": managed_symbol,
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "message_id": message_id,
                        "retcode": retcode,
                        "cancel_text": text,
                        "cancel_review": cancel_review.as_dict(),
                        "managed": managed,
                    },
                )
                if retcode in {10008, 10009}:
                    managed["channel_cancel_removed"] = True
                    managed["channel_cancel_removed_utc"] = datetime.now(UTC).isoformat()
                    managed["channel_cancel_message_id"] = int(message_id or 0)
                    managed["channel_cancel_retcode"] = retcode
                    removed += 1
                    log.info(f"[CANCEL] channel message removed pending {ticket} for {signal_id}: retcode={retcode}")
                else:
                    log.warning(f"[CANCEL] channel pending remove rejected {ticket} for {signal_id}: retcode={retcode}")
        if removed:
            _save_managed_state(managed_path, managed_state)
        return removed

    def _mark_hold_for_chat(
        chat_id: int | None,
        chat_title: str,
        text: str,
        message_id: int,
        reply_to_message_id: int = 0,
    ) -> int:
        signals = managed_state.setdefault("signals", {})
        normalized_title = str(chat_title or "").strip().lower()
        active_position_tickets = set()
        active_order_tickets = set()
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                active_position_tickets.add(int(getattr(position, "ticket", 0) or 0))
            for order in orders_by_magic(managed_symbol, cfg.magic):
                active_order_tickets.add(int(getattr(order, "ticket", 0) or 0))

        now_iso = datetime.now(UTC).isoformat()
        marked = 0
        active_rows = [
            managed
            for managed in signals.values()
            if (
                int(managed.get("order_ticket", 0) or 0) in active_order_tickets
                or int(managed.get("order_ticket", 0) or 0) in active_position_tickets
                or int(managed.get("deal_ticket", 0) or 0) in active_position_tickets
            )
        ]
        target_message_ids = _channel_update_target_message_ids(
            active_rows,
            chat_id,
            chat_title,
            message_id,
            reply_to_message_id,
        )
        if not target_message_ids:
            return 0
        for signal_id, managed in list(signals.items()):
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if not (same_chat or same_title):
                continue
            if not (_managed_source_message_ids(managed) & target_message_ids):
                continue

            order_ticket = int(managed.get("order_ticket", 0) or 0)
            deal_ticket = int(managed.get("deal_ticket", 0) or 0)
            is_active = order_ticket in active_order_tickets or order_ticket in active_position_tickets or deal_ticket in active_position_tickets
            if not is_active:
                continue

            managed["hold_seen"] = True
            managed["hold_seen_utc"] = now_iso
            managed["hold_message_id"] = int(message_id or 0)
            managed["hold_text"] = text[:500]
            marked += 1

        _append_jsonl(
            events_path,
            {
                "type": "channel_hold_seen",
                "chat_id": chat_id,
                "chat_title": chat_title,
                "message_id": int(message_id or 0),
                "marked_active": marked,
                "text": text,
            },
        )
        if marked:
            _save_managed_state(managed_path, managed_state)
        return marked

    def _protect_positions_for_chat(
        chat_id: int | None,
        chat_title: str,
        text: str,
        message_id: int,
        reply_to_message_id: int = 0,
    ) -> int:
        """Apply an explicit provider secure/BE update to the matching signal cycle."""
        signals = managed_state.setdefault("signals", {})
        normalized_title = str(chat_title or "").strip().lower()
        positions_by_ticket = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions_by_ticket[int(getattr(position, "ticket", 0) or 0)] = position
        active_rows = [
            managed
            for managed in signals.values()
            if (
                int(managed.get("order_ticket", 0) or 0) in positions_by_ticket
                or int(managed.get("deal_ticket", 0) or 0) in positions_by_ticket
            )
        ]
        target_message_ids = _channel_update_target_message_ids(
            active_rows,
            chat_id,
            chat_title,
            message_id,
            reply_to_message_id,
        )
        if not target_message_ids:
            return 0

        buffer_usd = max(
            0.0,
            _env_float(
                "PHOENIX_PROVIDER_BE_BUFFER_USD" if _is_phoenix_source(chat_id, chat_title) else "SIGNAL_PROVIDER_BE_BUFFER_USD",
                0.05 if _is_phoenix_source(chat_id, chat_title) else 0.0,
            ),
        )
        changed = 0
        now_iso = datetime.now(UTC).isoformat()
        for signal_id, managed in list(signals.items()):
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if not (same_chat or same_title):
                continue
            if not (_managed_source_message_ids(managed) & target_message_ids):
                continue

            position_ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            position = positions_by_ticket.get(position_ticket)
            if position is None:
                continue
            side = str(managed.get("side", "") or "").lower()
            entry = float(managed.get("entry", 0.0) or getattr(position, "price_open", 0.0) or 0.0)
            if side not in {"buy", "sell"} or entry <= 0.0:
                continue
            current_sl = float(getattr(position, "sl", 0.0) or 0.0)
            requested_sl = entry + buffer_usd if side == "buy" else entry - buffer_usd
            requested_sl = _better_stop(side, current_sl, requested_sl)
            position_symbol = str(getattr(position, "symbol", "") or managed.get("symbol", "") or symbol_by_asset["gold"])
            tick = get_tick(position_symbol)
            current_price = float(tick.bid if side == "buy" else tick.ask)
            if _defer_ghp_currency_provider_be(chat_id, managed, current_price):
                _append_jsonl(
                    events_path,
                    {
                        "type": "channel_secure_be_deferred_until_tp1",
                        "signal_id": signal_id,
                        "position_ticket": position_ticket,
                        "current_price": current_price,
                        "tp1": float(managed.get("tps", [0.0])[0]),
                        "message_id": int(message_id or 0),
                    },
                )
                log.info(
                    f"[GHP CURRENCY] deferred provider BE before TP1 for {signal_id}"
                )
                continue
            if (side == "buy" and requested_sl >= current_price) or (side == "sell" and requested_sl <= current_price):
                continue
            broker_safe = _minimum_safe_stop(
                position_symbol,
                side,
                current_price,
                requested_sl,
                current_spread_points(position_symbol),
                cfg.signal_sl_min_points,
            )
            if (side == "buy" and broker_safe < requested_sl - 0.005) or (
                side == "sell" and broker_safe > requested_sl + 0.005
            ):
                continue
            if current_sl > 0.0 and abs(broker_safe - current_sl) < 0.01:
                continue
            result = modify_position(
                position,
                sl=broker_safe,
                tp=float(getattr(position, "tp", 0.0) or managed.get("execution_tp", 0.0) or 0.0),
            )
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "channel_secure_be_attempt",
                    "signal_id": signal_id,
                    "position_ticket": position_ticket,
                    "old_sl": current_sl,
                    "new_sl": broker_safe,
                    "buffer_usd": buffer_usd,
                    "message_id": int(message_id or 0),
                    "reply_to_message_id": int(reply_to_message_id or 0),
                    "retcode": retcode,
                    "text": text,
                },
            )
            if retcode in {10008, 10009}:
                managed["provider_be_seen"] = True
                managed["provider_be_seen_utc"] = now_iso
                managed["provider_be_message_id"] = int(message_id or 0)
                managed["protected_to_tp1"] = True
                changed += 1
        if changed:
            _save_managed_state(managed_path, managed_state)
        return changed

    def _update_ghp_stop_for_chat(
        chat_id: int | None,
        chat_title: str,
        text: str,
        message_id: int,
        reply_to_message_id: int = 0,
    ) -> int:
        """Apply a standalone GHP SL update to the addressed active cycle."""
        update = parse_ghp_message(text, chat_title)
        requested_sl = float(update.stop_loss or 0.0)
        if update.kind != "sl_update" or requested_sl <= 0.0:
            return 0
        signals = managed_state.setdefault("signals", {})
        normalized_title = str(chat_title or "").strip().lower()
        positions_by_ticket = {}
        orders_by_ticket = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions_by_ticket[int(getattr(position, "ticket", 0) or 0)] = position
            for order in orders_by_magic(managed_symbol, cfg.magic):
                orders_by_ticket[int(getattr(order, "ticket", 0) or 0)] = order
        active_rows = [
            managed
            for managed in signals.values()
            if int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0) in positions_by_ticket
            or int(managed.get("order_ticket", 0) or 0) in orders_by_ticket
        ]
        target_message_ids = _channel_update_target_message_ids(
            active_rows, chat_id, chat_title, message_id, reply_to_message_id
        )
        if not target_message_ids:
            return 0

        changed = 0
        for signal_id, managed in list(signals.items()):
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if not (same_chat or same_title):
                continue
            if not (_managed_source_message_ids(managed) & target_message_ids):
                continue
            if update.asset and str(managed.get("asset", "") or "") != update.asset:
                continue
            if update.side and str(managed.get("side", "") or "") != update.side:
                continue
            side = str(managed.get("side", "") or "").lower()
            if side not in {"buy", "sell"}:
                continue
            ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            position = positions_by_ticket.get(ticket)
            order = orders_by_ticket.get(int(managed.get("order_ticket", 0) or 0))
            result = None
            old_sl = 0.0
            target_type = ""
            if position is not None:
                entry = float(getattr(position, "price_open", 0.0) or 0.0)
                old_sl = float(getattr(position, "sl", 0.0) or 0.0)
                if not _valid_stop_for_side(side, entry, requested_sl) or _sl_increases_risk(side, old_sl, requested_sl):
                    continue
                result = modify_position(
                    position,
                    sl=requested_sl,
                    tp=float(getattr(position, "tp", 0.0) or managed.get("execution_tp", 0.0) or 0.0),
                )
                target_type = "position"
            elif order is not None:
                entry = float(getattr(order, "price_open", 0.0) or 0.0)
                old_sl = float(getattr(order, "sl", 0.0) or 0.0)
                if not _valid_stop_for_side(side, entry, requested_sl) or _sl_increases_risk(side, old_sl, requested_sl):
                    continue
                result = modify_order(
                    order,
                    price=entry,
                    sl=requested_sl,
                    tp=float(getattr(order, "tp", 0.0) or managed.get("execution_tp", 0.0) or 0.0),
                    magic=cfg.magic,
                )
                target_type = "pending"
            if result is None:
                continue
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "ghp_provider_sl_update_attempt",
                    "target_type": target_type,
                    "signal_id": signal_id,
                    "ticket": ticket,
                    "old_sl": old_sl,
                    "new_sl": requested_sl,
                    "retcode": retcode,
                    "text": text,
                },
            )
            if retcode in {10008, 10009}:
                managed["initial_sl"] = requested_sl
                managed["ghp_provider_sl_update_utc"] = datetime.now(UTC).isoformat()
                changed += 1
        if changed:
            _save_managed_state(managed_path, managed_state)
        return changed

    def _close_ghp_positions_for_chat(
        chat_id: int | None,
        chat_title: str,
        text: str,
        message_id: int,
        reply_to_message_id: int = 0,
        *,
        worst_only: bool = False,
    ) -> int:
        """Close only the GHP cycle addressed by a provider close message."""
        signals = managed_state.setdefault("signals", {})
        normalized_title = str(chat_title or "").strip().lower()
        positions_by_ticket = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions_by_ticket[int(getattr(position, "ticket", 0) or 0)] = position
        active_rows = [
            managed
            for managed in signals.values()
            if (
                int(managed.get("order_ticket", 0) or 0) in positions_by_ticket
                or int(managed.get("deal_ticket", 0) or 0) in positions_by_ticket
            )
        ]
        target_message_ids = _channel_update_target_message_ids(
            active_rows,
            chat_id,
            chat_title,
            message_id,
            reply_to_message_id,
        )
        if not target_message_ids:
            return 0

        parsed_update = parse_ghp_message(text, chat_title)
        candidates = []
        for signal_id, managed in list(signals.items()):
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if not (same_chat or same_title):
                continue
            if not (_managed_source_message_ids(managed) & target_message_ids):
                continue
            if parsed_update.asset and str(managed.get("asset", "") or "") != parsed_update.asset:
                continue
            if parsed_update.side and str(managed.get("side", "") or "") != parsed_update.side:
                continue
            ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            position = positions_by_ticket.get(ticket)
            if position is not None:
                candidates.append((signal_id, managed, position))

        if worst_only and candidates:
            candidates = [min(candidates, key=lambda row: float(getattr(row[2], "profit", 0.0) or 0.0))]

        closed = 0
        now_iso = datetime.now(UTC).isoformat()
        for signal_id, managed, position in candidates:
            if _hold_remaining_seconds(position, managed) > 0:
                continue
            result = close_position(position, cfg.deviation)
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "ghp_provider_close_attempt",
                    "signal_id": signal_id,
                    "position_ticket": int(getattr(position, "ticket", 0) or 0),
                    "worst_only": bool(worst_only),
                    "profit": float(getattr(position, "profit", 0.0) or 0.0),
                    "retcode": retcode,
                    "text": text,
                },
            )
            if retcode in {10008, 10009}:
                managed["ghp_provider_closed"] = True
                managed["ghp_provider_closed_utc"] = now_iso
                managed["ghp_provider_close_message_id"] = int(message_id or 0)
                closed += 1
        if closed:
            _save_managed_state(managed_path, managed_state)
        return closed

    def _managed_target_level(managed: dict) -> int:
        try:
            execution_tp = float(managed.get("execution_tp", 0.0) or 0.0)
            tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
            for index, tp in enumerate(tps, start=1):
                if abs(float(tp) - execution_tp) < 0.05:
                    return index
        except Exception:
            pass
        try:
            plan_index = int(managed.get("target_plan_index", 0) or 0)
        except Exception:
            plan_index = 0
        if "phoenix" in str(managed.get("strategy", "") or "").lower():
            return {1: 1, 2: 1, 3: 2, 4: 4, 5: 6}.get(plan_index, plan_index)
        return max(1, plan_index)

    def _apply_channel_tp_hit(
        chat_id: int | None,
        chat_title: str,
        text: str,
        message_id: int,
        reply_to_message_id: int = 0,
    ) -> int:
        hit_level = _tp_hit_level(text)
        if hit_level <= 0:
            return 0
        if _is_phoenix_source(chat_id, chat_title) and _env_bool(
            "PHOENIX_TP_HIT_CANCEL_PENDING_ENABLED", False
        ):
            actions = _cancel_phoenix_range_pending_for_chat(
                chat_id,
                f"provider_tp{hit_level}_hit",
                message_id,
            )
        else:
            actions = 0
        signals = managed_state.setdefault("signals", {})
        normalized_title = str(chat_title or "").strip().lower()
        positions_by_ticket = {}
        orders_by_ticket = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions_by_ticket[int(getattr(position, "ticket", 0) or 0)] = position
            for order in orders_by_magic(managed_symbol, cfg.magic):
                orders_by_ticket[int(getattr(order, "ticket", 0) or 0)] = order

        changed = False
        now_iso = datetime.now(UTC).isoformat()
        active_rows = [
            managed
            for managed in signals.values()
            if (
                int(managed.get("order_ticket", 0) or 0) in orders_by_ticket
                or int(managed.get("order_ticket", 0) or 0) in positions_by_ticket
                or int(managed.get("deal_ticket", 0) or 0) in positions_by_ticket
            )
        ]
        target_message_ids = _channel_update_target_message_ids(
            active_rows,
            chat_id,
            chat_title,
            message_id,
            reply_to_message_id,
        )
        if not target_message_ids:
            log.info(
                f"[TP-HIT] ignored unscoped update message={message_id} "
                f"reply_to={reply_to_message_id} level={hit_level}"
            )
            return actions
        for signal_id, managed in list(signals.items()):
            managed_chat_id = int(managed.get("chat_id", 0) or 0)
            managed_title = str(managed.get("chat_title", "") or "").strip().lower()
            same_chat = managed_chat_id != 0 and int(chat_id or 0) == managed_chat_id
            same_title = bool(normalized_title and managed_title and normalized_title == managed_title)
            if not (same_chat or same_title):
                continue
            if not (_managed_source_message_ids(managed) & target_message_ids):
                continue

            target_level = _managed_target_level(managed)
            effective_hit_level = 99 if hit_level >= 99 else hit_level
            close_this_level = effective_hit_level >= 99 or target_level <= effective_hit_level
            position_ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            position = positions_by_ticket.get(position_ticket)
            order = orders_by_ticket.get(int(managed.get("order_ticket", 0) or 0))

            if order is not None:
                if bool(managed.get("ignore_channel_pending_cancel", False)):
                    continue
                is_phoenix_managed = _is_phoenix_source(
                    managed.get("chat_id"),
                    str(managed.get("chat_title", "") or ""),
                )
                if is_phoenix_managed and not _env_bool(
                    "PHOENIX_TP_HIT_CANCEL_PENDING_ENABLED", False
                ):
                    continue
                cancel_tp_index = _pending_cancel_tp_index(managed)
                if effective_hit_level < 99 and effective_hit_level < cancel_tp_index:
                    continue
                result = remove_order(order, magic=cfg.magic)
                retcode = getattr(result, "retcode", None)
                actions += 1
                _append_jsonl(
                    events_path,
                    {
                        "type": "channel_tp_hit_pending_remove_attempt",
                        "hit_level": hit_level,
                        "effective_hit_level": effective_hit_level,
                        "target_level": target_level,
                        "cancel_tp_index": cancel_tp_index,
                        "signal_id": signal_id,
                        "order_ticket": int(getattr(order, "ticket", 0) or 0),
                        "retcode": retcode,
                        "text": text,
                    },
                )
                if retcode in {10008, 10009}:
                    managed["channel_tp_removed_pending"] = True
                    managed["channel_tp_hit_utc"] = now_iso
                    changed = True
                continue

            if position is None:
                continue
            if close_this_level:
                position_profit = float(getattr(position, "profit", 0.0) or 0.0)
                if effective_hit_level < 99 and position_profit <= 0.0:
                    _append_jsonl(
                        events_path,
                        {
                            "type": "channel_tp_hit_close_skipped_negative",
                            "hit_level": hit_level,
                            "effective_hit_level": effective_hit_level,
                            "target_level": target_level,
                            "signal_id": signal_id,
                            "position_ticket": int(getattr(position, "ticket", 0) or 0),
                            "profit": position_profit,
                            "reason": "tp_update_does_not_match_losing_position",
                            "text": text,
                        },
                    )
                    log.warning(
                        f"[TP-HIT] ignored mismatched TP update for losing position: "
                        f"signal={signal_id} level={effective_hit_level} profit={position_profit:.2f}"
                    )
                    continue
                remaining = _hold_remaining_seconds(position, managed)
                if remaining > 0:
                    _append_jsonl(
                        events_path,
                        {
                            "type": "channel_tp_hit_close_deferred_min_hold",
                            "hit_level": hit_level,
                            "effective_hit_level": effective_hit_level,
                            "target_level": target_level,
                            "signal_id": signal_id,
                            "position_ticket": int(getattr(position, "ticket", 0) or 0),
                            "remaining_seconds": round(remaining, 1),
                            "text": text,
                        },
                    )
                    managed["deferred_channel_tp_hit_level"] = int(effective_hit_level)
                    managed["deferred_channel_tp_hit_utc"] = now_iso
                    changed = True
                    continue
                result = close_position(position, cfg.deviation)
                retcode = getattr(result, "retcode", None)
                actions += 1
                _append_jsonl(
                    events_path,
                    {
                        "type": "channel_tp_hit_close_attempt",
                        "hit_level": hit_level,
                        "effective_hit_level": effective_hit_level,
                        "target_level": target_level,
                        "signal_id": signal_id,
                        "position_ticket": int(getattr(position, "ticket", 0) or 0),
                        "profit": float(getattr(position, "profit", 0.0) or 0.0),
                        "retcode": retcode,
                        "text": text,
                    },
                )
                if retcode in {10008, 10009}:
                    managed["channel_tp_closed"] = True
                    managed["channel_tp_hit_utc"] = now_iso
                    managed["channel_tp_hit_level"] = int(effective_hit_level)
                    changed = True
                continue

            if hit_level >= 1:
                if _hold_remaining_seconds(position, managed) > 0:
                    continue
                side = str(managed.get("side", ""))
                entry = float(managed.get("entry", 0.0) or getattr(position, "price_open", 0.0) or 0.0)
                current_sl = float(getattr(position, "sl", 0.0) or 0.0)
                protect_mode = str(managed.get("protect_mode", "") or "").lower()
                if protect_mode == "be_after_tp2" and hit_level < 2:
                    continue
                if protect_mode in {"be_after_tp3", "tp1_after_tp3"} and hit_level < 3:
                    continue
                if protect_mode == "phoenix_ladder":
                    new_sl = _phoenix_progressive_stop(side, entry, [float(value) for value in managed.get("tps", [])], hit_level, current_sl)
                    if new_sl is None:
                        continue
                elif protect_mode == "tp1_after_tp3":
                    tps = [float(value) for value in managed.get("tps", []) if float(value or 0.0) > 0]
                    if not tps:
                        continue
                    new_sl = _better_stop(side, current_sl, float(tps[0]))
                else:
                    new_sl = _better_stop(side, current_sl, entry)
                if current_sl <= 0 or abs(new_sl - current_sl) >= 0.01:
                    result = modify_position(position, sl=new_sl, tp=float(getattr(position, "tp", 0.0) or 0.0))
                    retcode = getattr(result, "retcode", None)
                    actions += 1
                    _append_jsonl(
                        events_path,
                        {
                            "type": "channel_tp_hit_be_attempt",
                            "hit_level": hit_level,
                            "target_level": target_level,
                            "signal_id": signal_id,
                            "position_ticket": int(getattr(position, "ticket", 0) or 0),
                            "new_sl": new_sl,
                            "retcode": retcode,
                            "text": text,
                        },
                    )
                    if retcode in {10008, 10009}:
                        managed["protected_to_tp1"] = True
                        if protect_mode == "phoenix_ladder":
                            managed["phoenix_ladder_level"] = int(hit_level)
                        managed["channel_tp_hit_utc"] = now_iso
                        changed = True
        if actions:
            _append_jsonl(
                events_path,
                {
                    "type": "channel_tp_hit_seen",
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "message_id": int(message_id or 0),
                    "hit_level": hit_level,
                    "actions": actions,
                    "text": text,
                },
            )
        if changed:
            _save_managed_state(managed_path, managed_state)
        return actions

    def _target_index_for_managed(managed: dict) -> int:
        try:
            saved_target = int(managed.get("tp_target_index", 0) or 0)
        except Exception:
            saved_target = 0
        if saved_target > 0:
            return saved_target
        try:
            plan_index = int(managed.get("target_plan_index", 1) or 0)
        except Exception:
            plan_index = 0
        if 1 <= plan_index <= len(THREE_LEG_TARGET_PLAN):
            return int(THREE_LEG_TARGET_PLAN[plan_index - 1][0])
        return max(1, int(managed.get("tp_target_index", 1) or 1))

    def _edited_signal_levels_for_managed(signal: ParsedSignal, managed: dict) -> tuple[float, float] | None:
        entry = float(managed.get("entry", signal.entry) or signal.entry)
        target_index = _target_index_for_managed(managed)
        edit_signal = signal
        if _is_phoenix_source(signal.chat_id, signal.chat_title):
            clean_entries = [float(value) for value in signal.entries if float(value or 0.0) > 0.0]
            for delta in [float(value) for value in range(-300, 301, 10) if value != 0]:
                shifted_entries = [round(float(value) + delta, 2) for value in clean_entries]
                if any(abs(float(value) - entry) < 0.05 for value in shifted_entries):
                    shifted_tps = [round(float(value) + delta, 2) for value in signal.tps]
                    shifted_sl = round(float(signal.sl or 0.0) + delta, 2) if signal.sl > 0 else signal.sl
                    if _level_ladder_valid_for_side(signal.side, shifted_entries, shifted_sl, shifted_tps):
                        edit_signal = replace(
                            signal,
                            entry=round(float(signal.entry or clean_entries[0]) + delta, 2),
                            entries=shifted_entries,
                            sl=shifted_sl,
                            tp=shifted_tps[0] if shifted_tps else signal.tp,
                            tps=shifted_tps,
                        )
                    break
        edit_tps = [float(value) for value in edit_signal.tps]
        if _is_phoenix_source(edit_signal.chat_id, edit_signal.chat_title):
            edit_tps = _repair_tps_for_entry(edit_signal.side, entry, edit_tps, max(target_index, 4))
        try:
            _, execution_tp, _, _ = _select_live_tps(edit_signal.side, entry, edit_tps, target_index)
        except Exception:
            execution_tp = float(managed.get("execution_tp", 0.0) or 0.0)
        new_sl = float(edit_signal.sl or 0.0)
        if new_sl <= 0:
            new_sl = float(managed.get("initial_sl", 0.0) or 0.0)
        if new_sl <= 0 or execution_tp <= 0:
            return None
        if _invalid_levels_reason(edit_signal.side, entry, new_sl, execution_tp):
            return None
        return new_sl, execution_tp

    def _apply_phoenix_followup_revision(signal: ParsedSignal) -> tuple[int, int]:
        signals = managed_state.setdefault("signals", {})
        now = datetime.now(UTC)
        window_seconds = max(1.0, _env_float("PHOENIX_REVISION_WINDOW_SECONDS", 120.0))
        candidates = [
            (signal_id, managed)
            for signal_id, managed in list(signals.items())
            if _phoenix_followup_revision_match(
                managed,
                signal,
                now=now,
                window_seconds=window_seconds,
                tp1_tolerance=_env_float("PHOENIX_REVISION_TP1_TOLERANCE_USD", 0.10),
                entry_tolerance=_env_float("PHOENIX_REVISION_ENTRY_TOLERANCE_USD", 2.0),
            )
        ]
        if not candidates:
            return 0, 0

        positions_by_ticket = {}
        orders_by_ticket = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions_by_ticket[int(getattr(position, "ticket", 0) or 0)] = position
            for order in orders_by_magic(managed_symbol, cfg.magic):
                orders_by_ticket[int(getattr(order, "ticket", 0) or 0)] = order

        active_candidates = []
        for signal_id, managed in candidates:
            position_ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            order_ticket = int(managed.get("order_ticket", 0) or 0)
            if position_ticket in positions_by_ticket or order_ticket in orders_by_ticket:
                active_candidates.append((signal_id, managed))
        if not active_candidates:
            return 0, 0

        newest_source_message = max(_managed_source_message_id(managed) for _, managed in active_candidates)
        matched = [
            (signal_id, managed)
            for signal_id, managed in active_candidates
            if _managed_source_message_id(managed) == newest_source_message
        ]
        changed_count = 0
        now_iso = now.isoformat()
        for signal_id, managed in matched:
            aliases = set(_managed_source_message_ids(managed))
            aliases.add(int(signal.message_id or 0))
            managed["provider_revision_message_ids"] = sorted(message_id for message_id in aliases if message_id > 0)
            managed["provider_revision_message_id"] = int(signal.message_id or 0)
            managed["provider_revision_seen_utc"] = now_iso

            levels = _edited_signal_levels_for_managed(signal, managed)
            if levels is None:
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_followup_revision_levels_skipped",
                        "reason": "invalid_or_missing_levels",
                        "signal_id": signal_id,
                        "revision_signal": signal.as_dict(),
                    },
                )
                continue
            new_sl, new_tp = levels
            position_ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            order_ticket = int(managed.get("order_ticket", 0) or 0)
            position = positions_by_ticket.get(position_ticket)
            order = orders_by_ticket.get(order_ticket)
            if position is not None:
                old_sl = float(getattr(position, "sl", 0.0) or 0.0)
                old_tp = float(getattr(position, "tp", 0.0) or 0.0)
                if abs(old_sl - new_sl) < 0.01 and abs(old_tp - new_tp) < 0.01:
                    continue
                result = modify_position(position, sl=new_sl, tp=new_tp)
                target_type = "position"
                ticket = position_ticket
            elif order is not None:
                old_sl = float(getattr(order, "sl", 0.0) or 0.0)
                old_tp = float(getattr(order, "tp", 0.0) or 0.0)
                if abs(old_sl - new_sl) < 0.01 and abs(old_tp - new_tp) < 0.01:
                    continue
                result = modify_order(
                    order,
                    price=float(getattr(order, "price_open", 0.0) or 0.0),
                    sl=new_sl,
                    tp=new_tp,
                    magic=cfg.magic,
                )
                target_type = "pending"
                ticket = order_ticket
            else:
                continue
            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_followup_revision_modify_attempt",
                    "target_type": target_type,
                    "signal_id": signal_id,
                    "ticket": ticket,
                    "old_message_id": newest_source_message,
                    "revision_message_id": int(signal.message_id or 0),
                    "old_sl": old_sl,
                    "new_sl": new_sl,
                    "old_tp": old_tp,
                    "new_tp": new_tp,
                    "retcode": retcode,
                },
            )
            if retcode in {10008, 10009}:
                managed["initial_sl"] = float(new_sl)
                managed["execution_tp"] = float(new_tp)
                managed["tps"] = [float(value) for value in signal.tps]
                managed["provider_revision_retcode"] = retcode
                changed_count += 1
            else:
                log.warning(
                    f"[PHOENIX REVISION] modify rejected signal={signal_id} "
                    f"ticket={ticket} retcode={retcode}"
                )
        _save_managed_state(managed_path, managed_state)
        return len(matched), changed_count

    def _apply_edited_signal_levels(signal: ParsedSignal) -> int:
        signals = managed_state.setdefault("signals", {})
        matched = [
            (signal_id, managed)
            for signal_id, managed in list(signals.items())
            if int(managed.get("chat_id", 0) or 0) == int(signal.chat_id or 0)
            and int(str(managed.get("signal_uid", "0:0")).split(":", 2)[1] or 0) == int(signal.message_id or 0)
        ]
        if not matched:
            return 0

        positions_by_ticket = {}
        orders_by_ticket = {}
        for managed_symbol in _active_symbols():
            for position in positions_by_magic(managed_symbol, cfg.magic):
                positions_by_ticket[int(getattr(position, "ticket", 0) or 0)] = position
            for order in orders_by_magic(managed_symbol, cfg.magic):
                orders_by_ticket[int(getattr(order, "ticket", 0) or 0)] = order

        changed_count = 0
        now_iso = datetime.now(UTC).isoformat()
        for signal_id, managed in matched:
            levels = _edited_signal_levels_for_managed(signal, managed)
            if levels is None:
                _append_jsonl(
                    events_path,
                    {
                        "type": "edited_signal_levels_skipped",
                        "reason": "invalid_or_missing_levels",
                        "signal_id": signal_id,
                        "edited_signal": signal.as_dict(),
                        "managed": managed,
                    },
                )
                continue
            new_sl, new_tp = levels
            position_ticket = int(managed.get("order_ticket", 0) or managed.get("deal_ticket", 0) or 0)
            position = positions_by_ticket.get(position_ticket)
            order_ticket = int(managed.get("order_ticket", 0) or 0)
            order = orders_by_ticket.get(order_ticket)

            result = None
            target_type = ""
            if position is not None:
                old_sl = float(getattr(position, "sl", 0.0) or 0.0)
                old_tp = float(getattr(position, "tp", 0.0) or 0.0)
                if _sl_increases_risk(signal.side, old_sl, new_sl):
                    _append_jsonl(
                        events_path,
                        {
                            "type": "edited_signal_sl_widen_skipped",
                            "target_type": "position",
                            "signal_id": signal_id,
                            "ticket": position_ticket,
                            "old_sl": old_sl,
                            "new_sl": new_sl,
                            "kept_sl": old_sl,
                            "new_tp": new_tp,
                            "edited_signal": signal.as_dict(),
                        },
                    )
                    new_sl = old_sl
                if abs(old_sl - new_sl) < 0.01 and abs(old_tp - new_tp) < 0.01:
                    continue
                result = modify_position(position, sl=new_sl, tp=new_tp)
                target_type = "position"
            elif order is not None:
                old_sl = float(getattr(order, "sl", 0.0) or 0.0)
                old_tp = float(getattr(order, "tp", 0.0) or 0.0)
                if _sl_increases_risk(signal.side, old_sl, new_sl):
                    _append_jsonl(
                        events_path,
                        {
                            "type": "edited_signal_sl_widen_skipped",
                            "target_type": "pending",
                            "signal_id": signal_id,
                            "ticket": order_ticket,
                            "old_sl": old_sl,
                            "new_sl": new_sl,
                            "kept_sl": old_sl,
                            "new_tp": new_tp,
                            "edited_signal": signal.as_dict(),
                        },
                    )
                    new_sl = old_sl
                if abs(old_sl - new_sl) < 0.01 and abs(old_tp - new_tp) < 0.01:
                    continue
                result = modify_order(order, price=float(getattr(order, "price_open", 0.0) or 0.0), sl=new_sl, tp=new_tp, magic=cfg.magic)
                target_type = "pending"
            else:
                continue

            retcode = getattr(result, "retcode", None)
            _append_jsonl(
                events_path,
                {
                    "type": "edited_signal_modify_attempt",
                    "target_type": target_type,
                    "signal_id": signal_id,
                    "ticket": position_ticket if target_type == "position" else order_ticket,
                    "new_sl": new_sl,
                    "new_tp": new_tp,
                    "retcode": retcode,
                    "edited_signal": signal.as_dict(),
                },
            )
            if retcode in {10008, 10009}:
                managed["edited_update_utc"] = now_iso
                managed["edited_sl"] = float(new_sl)
                managed["edited_tp"] = float(new_tp)
                managed["edited_retcode"] = retcode
                managed["initial_sl"] = float(new_sl)
                managed["execution_tp"] = float(new_tp)
                managed["tps"] = [float(value) for value in signal.tps]
                changed_count += 1
                log.info(f"[EDIT] updated {target_type} levels for {signal_id}: sl={new_sl} tp={new_tp} retcode={retcode}")
            else:
                log.warning(f"[EDIT] modify rejected for {signal_id}: retcode={retcode}")
        if changed_count:
            _save_managed_state(managed_path, managed_state)
        return changed_count

    async def _handle_signal_event(event, *, edited: bool) -> None:
        chat = await event.get_chat()
        chat_id = getattr(event, "chat_id", None)
        username = getattr(chat, "username", None)
        if not _matches_channel(chat_id, username):
            return

        sender_id = getattr(event, "sender_id", None)
        post_author = str(getattr(event.message, "post_author", "") or "")
        if not _matches_sender(sender_id, post_author):
            return

        text = event.raw_text or ""
        chat_title = str(getattr(chat, "title", "") or username or chat_id or "unknown")
        reply_to_message_id = _telegram_reply_to_message_id(event.message)
        is_phoenix = _is_phoenix_source(chat_id, chat_title)
        announced_side = _phoenix_direction_hint(text) if is_phoenix else None
        if announced_side:
            _remember_phoenix_direction(chat_id, announced_side, int(event.id or 0))
            log.info(f"[PHOENIX] remembered announced direction={announced_side.upper()} for the next signal")
            if _matches_trade_channel(chat_id, username) and _is_phoenix_direction_runner_announcement(text):
                _open_phoenix_direction_runner(
                    chat_id,
                    chat_title,
                    int(event.id or 0),
                    announced_side,
                )
        if is_phoenix and _matches_trade_channel(chat_id, username):
            phoenix_range = _phoenix_numeric_range(text)
            fresh_side = _fresh_phoenix_direction(chat_id)
            if phoenix_range and fresh_side:
                if _env_bool("PHOENIX_DIRECTION_RUNNER_RANGE_RECONCILE_ENABLED", True):
                    _validate_phoenix_direction_runner_with_range(
                        chat_id,
                        fresh_side,
                        phoenix_range,
                    )
                # Runner validation and the staged range package are separate
                # parts of one Phoenix cycle. A confirmed runner must not
                # suppress the market/pending legs from the published range.
                range_opened = _open_phoenix_range_trigger(
                    chat_id,
                    chat_title,
                    int(event.id or 0),
                    fresh_side,
                    phoenix_range,
                )
                if range_opened:
                    _consume_phoenix_direction(chat_id)
        tp_actions = _apply_channel_tp_hit(
            chat_id,
            chat_title,
            text,
            int(event.id or 0),
            reply_to_message_id,
        )
        if tp_actions:
            log.info(f"[TP-HIT] channel TP update from hidden source triggered {tp_actions} actions")
            return

        ghp_update = parse_ghp_message(text, chat_title) if is_ghp_source(chat_id, chat_title) else None
        if ghp_update is not None and ghp_update.kind == "sl_update":
            changed = _update_ghp_stop_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            log.info(f"[GHP SL] provider update changed {changed} active stops")
            return
        if ghp_update is not None and ghp_update.kind in {"close", "close_partial"}:
            removed = _cancel_pending_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            closed = _close_ghp_positions_for_chat(
                chat_id,
                chat_title,
                text,
                int(event.id or 0),
                reply_to_message_id,
                worst_only=ghp_update.kind == "close_partial",
            )
            log.info(
                f"[GHP CLOSE] provider update removed {removed} pending and closed {closed} "
                f"positions (worst_only={ghp_update.kind == 'close_partial'})"
            )
            return
        if ghp_update is not None and ghp_update.kind == "sl_hit":
            removed = _cancel_pending_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            log.info(f"[GHP SL] provider SL update removed {removed} remaining pending orders")
            return

        if _is_secure_message(text):
            removed = 0
            if not is_phoenix or _env_bool(
                "PHOENIX_SECURE_CANCEL_PENDING_ENABLED", False
            ):
                removed = _cancel_pending_for_chat(
                    chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
                )
                if is_phoenix:
                    removed += _cancel_phoenix_range_pending_for_chat(
                        chat_id,
                        "provider_secure_be",
                        int(event.id or 0),
                    )
            marked = _mark_hold_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            protected = _protect_positions_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            _append_jsonl(
                events_path,
                {
                    "type": "channel_secure_cancel_pending_seen",
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "message_id": int(event.id or 0),
                    "removed_pending": removed,
                    "marked_active": marked,
                    "protected_positions": protected,
                    "edited": edited,
                    "text": text,
                },
            )
            log.info(
                f"[SECURE] secure/BE message removed {removed} pending, marked {marked} active "
                f"orders/positions and protected {protected} positions"
            )
            return

        if _is_hold_message(text):
            marked = _mark_hold_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            _append_jsonl(
                events_path,
                {
                    "type": "channel_hold_message_seen",
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "message_id": int(event.id or 0),
                    "marked_active": marked,
                    "edited": edited,
                    "text": text,
                },
            )
            log.info(f"[HOLD] hold message kept pending orders and marked {marked} active orders/positions")
            return

        if _is_cancel_message(text):
            removed = _cancel_pending_for_chat(
                chat_id, chat_title, text, int(event.id or 0), reply_to_message_id
            )
            _append_jsonl(
                events_path,
                {
                    "type": "channel_cancel_seen",
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "message_id": int(event.id or 0),
                    "removed_pending": removed,
                    "edited": edited,
                    "text": text,
                },
            )
            log.info(f"[CANCEL] cancel/update message from hidden source removed {removed} pending orders")
            return

        uid_base = f"{chat_id}:{int(event.id or 0)}"
        parser_text = _ghp_contextual_signal_text(
            chat_id,
            chat_title,
            int(event.id or 0),
            text,
        )
        signal = _parse_signal(
            parser_text,
            uid_base,
            chat_id,
            chat_title,
            post_author,
            int(event.id or 0),
            side_hint=_fresh_phoenix_direction(chat_id) if is_phoenix else None,
        )
        if signal is None:
            if _looks_like_signal_candidate(text, chat_title):
                _append_jsonl(
                    events_path,
                    {
                        "type": "unparsed_signal_candidate",
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "message_id": int(event.id or 0),
                        "edited": edited,
                        "text": text[:2000],
                    },
                )
                log.info(f"[PARSE] stored unparsed signal candidate from {chat_title} message={int(event.id or 0)}")
            return

        if is_phoenix and not _phoenix_tp_ladder_is_strict(signal.side, signal.tps):
            _append_jsonl(
                events_path,
                {
                    "type": "phoenix_signal_deferred_invalid_tp_ladder",
                    "chat_id": chat_id,
                    "message_id": int(event.id or 0),
                    "edited": edited,
                    "side": signal.side,
                    "tps": signal.tps,
                    "text": text[:2000],
                },
            )
            log.warning(
                f"[PHOENIX] deferred message={int(event.id or 0)} with duplicated/non-monotonic TP ladder; "
                "waiting for provider edit"
            )
            return

        if is_phoenix:
            revision_matches, revision_changes = _apply_phoenix_followup_revision(signal)
            if revision_matches:
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_followup_revision_seen",
                        "matched_legs": revision_matches,
                        "updated_legs": revision_changes,
                        "signal": signal.as_dict(),
                    },
                )
                _remember_signal_signature(signal)
                log.info(
                    f"[PHOENIX REVISION] message={int(signal.message_id or 0)} updated "
                    f"{revision_changes}/{revision_matches} active legs; duplicate package suppressed"
                )
                return
            _consume_phoenix_direction(chat_id)
            reconciled = _reconcile_phoenix_preliminary_positions(signal)
            if reconciled:
                log.info(f"[PHOENIX] reconciled {reconciled} preliminary positions/orders with the full signal")
            synced = _sync_phoenix_range_provider_sl(signal)
            if synced:
                log.info(f"[PHOENIX] applied provider SL={signal.sl:.2f} to {synced} preliminary range legs")
            preliminary_range = _phoenix_preliminary_range_covers_signal(signal)
            if preliminary_range:
                play_full_after_pre = _env_bool("PHOENIX_PLAY_FULL_SIGNAL_AFTER_PRE_RANGE", True)
                _append_jsonl(
                    events_path,
                    {
                        "type": "phoenix_full_signal_covered_by_preliminary_range",
                        "range_message_id": int(preliminary_range.get("message_id", 0) or 0),
                        "successful_legs": int(preliminary_range.get("successful_legs", 0) or 0),
                        "play_full_signal_after_pre_range": play_full_after_pre,
                        "signal": signal.as_dict(),
                    },
                )
                log.info(
                    f"[PHOENIX] full signal covered by preliminary range "
                    f"message={int(preliminary_range.get('message_id', 0) or 0)}; "
                    f"keeping existing {int(preliminary_range.get('successful_legs', 0) or 0)} legs; "
                    f"full_signal_after_pre={'ON' if play_full_after_pre else 'OFF'}"
                )
                if not play_full_after_pre:
                    return

        if edited:
            updated = _apply_edited_signal_levels(signal)
            message_key = f"{int(chat_id or 0)}:{int(event.id or 0)}"
            executed_before = any(
                _managed_message_key(managed) == message_key
                for managed in managed_state.setdefault("signals", {}).values()
            )
            execute_as_fresh = _should_execute_fresh_edited_signal(
                is_phoenix=is_phoenix,
                is_ghp_gold=signal.asset == "gold" and is_ghp_source(signal.chat_id, signal.chat_title),
                updated_positions=updated,
                message_date=getattr(event.message, "date", None),
            )
            if executed_before:
                execute_as_fresh = False
            _append_jsonl(
                events_path,
                {
                    "type": "edited_signal_seen",
                    "updated": updated,
                    "executed_before": executed_before,
                    "execute_as_fresh": execute_as_fresh,
                    "signal": signal.as_dict(),
                },
            )
            log.info(f"[EDIT] edited signal seen, updated {updated} active orders/positions")
            if not execute_as_fresh:
                if executed_before and updated == 0:
                    log.info(
                        f"[EDIT] message={int(event.id or 0)} was already executed; "
                        "not reopening a completed Phoenix trade"
                    )
                return
            log.info("[EDIT] fresh edited signal had no active trade; executing it as the initial signal")

        if not _matches_trade_channel(chat_id, username):
            signal_symbol = _symbol_for_signal(signal)
            market_price = None
            if signal_symbol:
                try:
                    tick = get_tick(signal_symbol)
                    market_price = _market_reference_price(signal.side, tick)
                except Exception:
                    market_price = None
            _append_jsonl(
                events_path,
                {
                    "type": "shadow_signal",
                    "mode": "observe_only",
                    "symbol": signal_symbol,
                    "market_price": market_price,
                    "signal": signal.as_dict(),
                },
            )
            processed.add(signal.uid)
            _save_runtime_state()
            log.info(f"[SHADOW] observed signal without execution: {chat_title} message={int(event.id or 0)}")
            return

        if signal.uid in processed:
            log.info(f"[SKIP] duplicate signal {signal.uid}")
            return
        if _is_recent_duplicate(signal):
            log.info(f"[SKIP] duplicate content for {signal.uid}")
            _append_jsonl(events_path, {"type": "skip", "reason": "duplicate_content", "signal": signal.as_dict()})
            return
        if await _stop_if_session_limit_reached():
            return

        state["last_signal"] = signal.as_dict()
        _save_runtime_state()
        signal_symbol = _symbol_for_signal(signal)
        if not signal_symbol:
            log.info(f"[SKIP] unsupported asset on this MT5 account: {signal.asset}")
            _append_jsonl(events_path, {"type": "skip", "reason": "unsupported_asset_symbol", "asset": signal.asset, "signal": signal.as_dict()})
            processed.add(signal.uid)
            _save_runtime_state()
            return
        log.info(f"[SIGNAL] {signal.side.upper()} {signal_symbol} market tp1={signal.tp} source=hidden")
        _append_jsonl(events_path, {"type": "signal", "signal": signal.as_dict()})
        await _place_signal(signal, str(username or ""))

    signal_event_lock = asyncio.Lock()

    async def _dispatch_signal_event(event, *, edited: bool) -> None:
        async with signal_event_lock:
            chat_id = int(getattr(event, "chat_id", 0) or 0)
            message_id = int(getattr(event, "id", 0) or 0)
            text = event.raw_text or ""
            channel_fingerprint_key = f"{chat_id}:{message_id}"
            fingerprint = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
            if message_id and channel_poll_fingerprints.get(channel_fingerprint_key) == fingerprint:
                return
            if chat_id == -1002864291293 and message_id:
                if phoenix_poll_fingerprints.get(str(message_id)) == fingerprint:
                    return
            await _handle_signal_event(event, edited=edited)
            _remember_channel_poll_fingerprint(chat_id, message_id, text)
            if chat_id == -1002864291293:
                _remember_phoenix_poll_fingerprint(message_id, text)

    @client.on(events.NewMessage)
    async def on_message(event) -> None:
        try:
            await _dispatch_signal_event(event, edited=False)
        except Exception as exc:
            log.warning(f"[TG] handler error: {type(exc).__name__}: {exc}")

    @client.on(events.MessageEdited)
    async def on_message_edited(event) -> None:
        try:
            await _dispatch_signal_event(event, edited=True)
        except Exception as exc:
            log.warning(f"[TG] handler error: {type(exc).__name__}: {exc}")

    class _PhoenixPolledEvent:
        def __init__(self, message, chat, chat_id: int):
            self.message = message
            self._chat = chat
            self.chat_id = int(chat_id)
            self.id = int(getattr(message, "id", 0) or 0)
            self.sender_id = getattr(message, "sender_id", None)
            self.raw_text = str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")

        async def get_chat(self):
            return self._chat

    async def _poll_phoenix_messages() -> None:
        if not _env_bool("PHOENIX_POLL_ENABLED", True):
            return
        channel_id = int(_env_float("PHOENIX_CHANNEL_ID", -1002864291293))
        interval = max(0.5, _env_float("PHOENIX_POLL_SECONDS", 1.0))
        max_age = max(30.0, _env_float("PHOENIX_POLL_MAX_AGE_SECONDS", 900.0))
        fetch_limit = max(5, min(50, int(_env_float("PHOENIX_POLL_MESSAGE_LIMIT", 15.0))))
        chat = None
        log.info(
            f"[PHOENIX] watchdog enabled: poll={interval:.1f}s max_age={max_age:.0f}s "
            f"messages={fetch_limit}"
        )
        while True:
            try:
                if chat is None:
                    chat = await client.get_entity(channel_id)
                messages = await client.get_messages(chat, limit=fetch_limit)
                now = datetime.now(UTC)
                for message in reversed(list(messages)):
                    message_id = int(getattr(message, "id", 0) or 0)
                    text = str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")
                    if not message_id or not text.strip():
                        continue
                    fingerprint = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
                    previous = phoenix_poll_fingerprints.get(str(message_id))
                    if previous == fingerprint:
                        continue
                    message_date = getattr(message, "date", None)
                    if message_date is not None:
                        if message_date.tzinfo is None:
                            message_date = message_date.replace(tzinfo=UTC)
                        age_seconds = max(0.0, (now - message_date.astimezone(UTC)).total_seconds())
                        if age_seconds > max_age:
                            _remember_phoenix_poll_fingerprint(message_id, text)
                            continue
                    event = _PhoenixPolledEvent(message, chat, channel_id)
                    log.info(
                        f"[PHOENIX] watchdog recovered message={message_id} "
                        f"edited={previous is not None}"
                    )
                    await _dispatch_signal_event(event, edited=previous is not None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                chat = None
                log.warning(f"[PHOENIX] watchdog error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(interval)

    async def _poll_trade_channel_messages() -> None:
        if not _env_bool("SIGNAL_CHANNEL_WATCHDOG_ENABLED", True):
            return
        interval = max(0.5, _env_float("SIGNAL_CHANNEL_WATCHDOG_SECONDS", 0.5))
        max_age = max(30.0, _env_float("SIGNAL_CHANNEL_WATCHDOG_MAX_AGE_SECONDS", 900.0))
        fetch_limit = max(3, min(20, int(_env_float("SIGNAL_CHANNEL_WATCHDOG_MESSAGE_LIMIT", 8.0))))
        channel_ids = sorted({int(value) for value in trade_raw_ids if int(value) < 0 and int(value) != -1002864291293})
        chats: dict[int, object] = {}
        log.info(
            f"[CHANNEL-WATCHDOG] enabled: channels={len(channel_ids)} poll={interval:.1f}s "
            f"max_age={max_age:.0f}s messages={fetch_limit}"
        )
        while True:
            try:
                now = datetime.now(UTC)
                for channel_id in channel_ids:
                    try:
                        chat = chats.get(channel_id)
                        if chat is None:
                            chat = await client.get_entity(channel_id)
                            chats[channel_id] = chat
                        messages = await client.get_messages(chat, limit=fetch_limit)
                        for message in reversed(list(messages)):
                            message_id = int(getattr(message, "id", 0) or 0)
                            text = str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")
                            if not message_id or not text.strip():
                                continue
                            key = f"{channel_id}:{message_id}"
                            fingerprint = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
                            previous = channel_poll_fingerprints.get(key)
                            if previous == fingerprint:
                                continue
                            message_date = getattr(message, "date", None)
                            if message_date is not None:
                                if message_date.tzinfo is None:
                                    message_date = message_date.replace(tzinfo=UTC)
                                age_seconds = max(0.0, (now - message_date.astimezone(UTC)).total_seconds())
                                if age_seconds > max_age:
                                    _remember_channel_poll_fingerprint(channel_id, message_id, text)
                                    continue
                            event = _PhoenixPolledEvent(message, chat, channel_id)
                            log.info(
                                f"[CHANNEL-WATCHDOG] recovered channel={channel_id} message={message_id} "
                                f"edited={previous is not None}"
                            )
                            await _dispatch_signal_event(event, edited=previous is not None)
                    except Exception as exc:
                        chats.pop(channel_id, None)
                        log.warning(
                            f"[CHANNEL-WATCHDOG] channel={channel_id} error: {type(exc).__name__}: {exc}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"[CHANNEL-WATCHDOG] loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(interval)

    async def _main() -> None:
        await _start_client()
        me = await client.get_me()
        user_label = getattr(me, "username", None) or getattr(me, "id", "unknown")
        log.info(f"Telegram user: {_masked(user_label)}")
        log.info("Telegram listener running. Press Ctrl+C to stop.")
        manager_task = asyncio.create_task(_manage_open_positions())
        phoenix_poll_task = asyncio.create_task(_poll_phoenix_messages())
        channel_poll_task = asyncio.create_task(_poll_trade_channel_messages())
        try:
            await client.run_until_disconnected()
        finally:
            manager_task.cancel()
            phoenix_poll_task.cancel()
            channel_poll_task.cancel()
            try:
                await manager_task
            except asyncio.CancelledError:
                pass
            try:
                await phoenix_poll_task
            except asyncio.CancelledError:
                pass
            try:
                await channel_poll_task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_main())
    finally:
        shutdown()


