from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


GHP_GOLD_CHAT_IDS = {-1002033681012, -1001958009741}
GHP_INDEX_CHAT_ID = -1003306025363
GHP_CURRENCY_CHAT_ID = -1003495213392
PHOENIX_CHAT_ID = -1002864291293

GHP_CURRENCY_ALLOWLIST = {
    "audusd",
    "chfjpy",
    "euraud",
    "eurjpy",
    "eurusd",
    "gbpjpy",
    "usdchf",
}
GHP_INDEX_ALLOWLIST = {"ger40"}


@dataclass(frozen=True)
class SignalReview:
    decision: str
    score: int
    reasons: tuple[str, ...]
    source_family: str
    execution_mode: str
    history_profile: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_review_policy(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def source_family(chat_id: int | None, title: str = "") -> str:
    value = int(chat_id or 0)
    normalized = str(title or "").lower()
    if value in GHP_GOLD_CHAT_IDS or "goldhunter" in normalized or "ghp" in normalized and "currency" not in normalized and "indices" not in normalized:
        return "ghp_gold"
    if value == GHP_INDEX_CHAT_ID or "ghp" in normalized and ("indices" in normalized or "crypto" in normalized):
        return "ghp_indices"
    if value == GHP_CURRENCY_CHAT_ID or "ghp" in normalized and "currency" in normalized:
        return "ghp_currency"
    if value == PHOENIX_CHAT_ID or "phoenix" in normalized:
        return "phoenix"
    return "other"


def _mixed_directions(text: str) -> bool:
    normalized = str(text or "")
    return bool(re.search(r"\b(?:buy|long)\b", normalized, re.I)) and bool(
        re.search(r"\b(?:sell|short)\b", normalized, re.I)
    )


def _past_tp1(side: str, market_price: float, tps: list[float]) -> bool:
    if market_price <= 0.0 or not tps:
        return False
    tp1 = float(tps[0])
    return market_price >= tp1 if side == "buy" else market_price <= tp1


def review_signal(
    *,
    chat_id: int | None,
    chat_title: str,
    asset: str,
    side: str,
    order_kind: str,
    entries: list[float],
    sl: float,
    tps: list[float],
    market_price: float,
    raw_text: str,
    policy: dict[str, Any] | None = None,
) -> SignalReview:
    del policy  # The policy file documents calibration; hard rules stay deterministic.
    family = source_family(chat_id, chat_title)
    reasons: list[str] = []
    score = 100
    mode = "normal"

    clean_entries = [float(value) for value in entries if float(value or 0.0) > 0.0]
    clean_tps = [float(value) for value in tps if float(value or 0.0) > 0.0]
    if side not in {"buy", "sell"} or not clean_entries or not clean_tps or float(sl or 0.0) <= 0.0:
        return SignalReview("reject", 0, ("incomplete_levels",), family, "none", "invalid")

    low, high = min(clean_entries), max(clean_entries)
    valid_sl = float(sl) < low if side == "buy" else float(sl) > high
    valid_tps = all(value > high for value in clean_tps) if side == "buy" else all(value < low for value in clean_tps)
    if not valid_sl or not valid_tps:
        return SignalReview("reject", 0, ("invalid_level_geometry",), family, "none", "invalid")

    if family.startswith("ghp") and _mixed_directions(raw_text):
        return SignalReview("reject", 0, ("mixed_direction_context",), family, "none", "ghp_100_sessions")

    if family == "ghp_currency" and asset not in GHP_CURRENCY_ALLOWLIST:
        return SignalReview("reject", 20, ("historically_negative_asset",), family, "none", "ghp_100_sessions")
    if family == "ghp_indices" and asset not in GHP_INDEX_ALLOWLIST:
        return SignalReview("reject", 25, ("weak_channel_asset_payoff",), family, "none", "ghp_100_sessions")

    # Never chase an already completed GHP move at market. A return to the
    # provider level may still be traded as a time-limited pending order.
    if family == "ghp_gold" and _past_tp1(side, market_price, clean_tps):
        mode = "provider_pending"
        reasons.append("market_already_past_tp1_wait_for_retrace")
        score -= 25

    wait_for_execution = bool(re.search(r"\bwait\s+for\s+execution\b", str(raw_text or ""), re.I))
    explicit_pending = str(order_kind or "").lower() in {"limit", "stop"}
    if family.startswith("ghp") and (wait_for_execution or explicit_pending):
        mode = "provider_pending"
        reasons.append("preserve_provider_entry")
    elif family == "ghp_gold" and market_price > 0.0:
        nearest = min(abs(float(entry) - market_price) for entry in clean_entries)
        if nearest > 2.0:
            mode = "provider_pending"
            reasons.append("market_outside_entry_tolerance")
            score -= 15

    if family == "phoenix":
        mode = "phoenix_zone"
        reasons.append("three_leg_validated_profile")
    elif family == "ghp_gold":
        reasons.append("tp1_tp2_deep_original_sl")
    elif family == "ghp_currency":
        reasons.append("tp1_only_currency_profile")
    elif family == "ghp_indices":
        reasons.append("tp1_only_ger40_profile")

    return SignalReview(
        "accept",
        max(0, score),
        tuple(reasons or ["standard_signal"]),
        family,
        mode,
        f"{family}_100_sessions" if family != "other" else "default",
    )
