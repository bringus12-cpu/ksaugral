from __future__ import annotations

import re
from dataclasses import dataclass


GHP_CHAT_IDS = {
    -1002033681012,
    -1001958009741,
    -1003306025363,
    -1003495213392,
}

NUMBER = r"\d{1,6}(?:[.,]\d{1,6})?"
NUMBER_RE = re.compile(rf"(?<![\w.])({NUMBER})(?![\w.])")
SIDE_RE = re.compile(r"\b(BUY(?:S|ING)?|SELL(?:S|ING)?|LONG|SHORT)\b", re.I)
RANGE_RE = re.compile(
    rf"\b(?:ENTRY|ZONE|AT)\b[^\d]{{0,16}}({NUMBER})\s*(?:-|/|\bTO\b)\s*({NUMBER})",
    re.I,
)
ENTRY_RE = re.compile(rf"\b(?:ENTRY|AT)\b\s*[:=@-]?\s*({NUMBER})", re.I)
SIDE_ENTRY_RE = re.compile(
    rf"\b(?:BUY(?:S|ING)?|SELL(?:S|ING)?|LONG|SHORT)\b(?:\s+(?:NOW|LIMIT|STOP))?\s+({NUMBER})",
    re.I,
)
FOCUS_ENTRY_RE = re.compile(rf"\bFOCUS\b\s*[:=@-]?\s*({NUMBER})", re.I)
PRICE_SIDE_RE = re.compile(rf"\b({NUMBER})\s+(?:BUY|SELL)\s+(?:ZONE|LIMIT|STOP)?\b", re.I)
PRE_SIDE_ENTRIES_RE = re.compile(
    rf"\b((?:{NUMBER})(?:\s*(?:-|/)\s*(?:{NUMBER}))+?)\s+"
    r"(?:BUY(?:S|ING)?|SELL(?:S|ING)?)\b",
    re.I,
)
SL_RE = re.compile(rf"\b(?:SL|S/L|STOP\s*LOSS)\b\s*[:=@-]?\s*({NUMBER})", re.I)
SL_UPDATE_RE = re.compile(
    rf"\b(?:MAKE|MOVE|SET|CHANGE|UPDATE)\s+(?:THE\s+)?(?:SL|S/L|STOP\s*LOSS)\b\s*(?:TO|AT|:|=)?\s*({NUMBER})",
    re.I,
)
TP_RE = re.compile(
    rf"\b(?:TP\s*\d*|TARGETS?\s*\d*|TAKE\s*PROFIT)\b\s*[:=@-]?\s*({NUMBER})",
    re.I,
)
SYMBOL_RE = re.compile(
    r"\b(XAUUSD|GOLD|BTCUSD|BITCOIN|NAS100|US100|USTEC|NDAQ|DJ30(?:\.S)?|US30|"
    r"GER40(?:\.S)?|DE40|DAX40|WTI|USOIL|"
    r"EURUSD|EURGBP|EURJPY|EURAUD|EURCAD|EURNZD|EURCHF|"
    r"AUDUSD|AUDJPY|AUDCAD|AUDNZD|AUDCHF|NZDUSD|NZDJPY|NZDCAD|NZDCHF|"
    r"USDCHF|USDJPY|USDCAD|GBPJPY|GBJPY|GBPUSD|GBPAUD|GBPCAD|GBPNZD|GBPCHF|CADJPY|CADCHF|CHFJPY)\b",
    re.I,
)


@dataclass(frozen=True)
class GhpSignal:
    asset: str
    symbol_token: str
    side: str
    order_kind: str
    entry: float
    entries: tuple[float, ...]
    sl: float
    tps: tuple[float, ...]


@dataclass(frozen=True)
class GhpMessage:
    kind: str
    signal: GhpSignal | None = None
    asset: str = ""
    side: str = ""
    tp_level: int = 0
    close_fraction: float = 0.0
    move_to_be: bool = False
    stop_loss: float = 0.0


def is_ghp_source(chat_id: int | None, chat_title: str = "", username: str = "") -> bool:
    if int(chat_id or 0) in GHP_CHAT_IDS:
        return True
    source = f"{chat_title} {username}".lower()
    return "goldhunter paul" in source or "ghp " in source or "ghptrading" in source


def _number(raw: str) -> float:
    token = str(raw or "").strip().replace(" ", "")
    if "," in token and "." not in token:
        left, right = token.split(",", 1)
        if left != "0" and len(left) >= 2 and len(right) == 3:
            token = left + right
        else:
            token = left + "." + right
    elif "," in token:
        token = token.replace(",", "")
    return float(token)


def _asset(symbol_token: str, chat_title: str) -> str:
    token = str(symbol_token or "").upper().replace(".S", "")
    aliases = {
        "XAUUSD": "gold",
        "GOLD": "gold",
        "BTCUSD": "btc",
        "BITCOIN": "btc",
        "NAS100": "nas100",
        "US100": "nas100",
        "USTEC": "nas100",
        "NDAQ": "nas100",
        "DJ30": "us30",
        "US30": "us30",
        "GER40": "ger40",
        "DE40": "ger40",
        "DAX40": "ger40",
        "WTI": "wti",
        "USOIL": "wti",
        "GBJPY": "gbpjpy",
    }
    if token in aliases:
        return aliases[token]
    if re.fullmatch(r"[A-Z]{6}", token):
        return token.lower()
    title = str(chat_title or "").lower()
    if "goldhunter" in title or "jackpot" in title and "currency" not in title and "indices" not in title:
        return "gold"
    return ""


def _reference_scale(asset: str) -> tuple[float, float]:
    if asset == "gold":
        return 1000.0, 10000.0
    if asset == "btc":
        return 10000.0, 500000.0
    if asset in {"nas100", "us30", "ger40"}:
        return 1000.0, 100000.0
    if asset == "wti":
        return 10.0, 1000.0
    return 0.00001, 1000.0


def _expand_level(value: float, anchor: float, asset: str) -> float:
    if value <= 0.0 or anchor <= 0.0:
        return value
    lower, upper = _reference_scale(asset)
    if lower <= value <= upper and abs(value - anchor) <= max(50.0, anchor * 0.25):
        return value
    candidates = {value}
    for power in range(-3, 5):
        candidates.add(value * (10.0**power))
    anchor_int = str(int(abs(anchor)))
    raw_int = str(int(abs(value)))
    if value.is_integer() and len(raw_int) < len(anchor_int):
        missing = len(anchor_int) - len(raw_int)
        for prefix_len in {missing, max(0, missing - 1), min(len(anchor_int), missing + 1)}:
            if prefix_len > 0:
                candidates.add(float(anchor_int[:prefix_len] + raw_int))
    valid = [candidate for candidate in candidates if lower <= candidate <= upper]
    if not valid:
        return value
    return min(valid, key=lambda candidate: (abs(candidate - anchor), abs(candidate - value)))


def _ordered_tps(side: str, entry: float, values: list[float]) -> tuple[float, ...]:
    unique = list(dict.fromkeys(round(float(value), 6) for value in values if float(value) > 0.0))
    if side == "buy":
        live = sorted(value for value in unique if value > entry)
    else:
        live = sorted((value for value in unique if value < entry), reverse=True)
    return tuple(live)


def _signal(text: str, chat_title: str) -> GhpSignal | None:
    normalized = " ".join(str(text or "").replace("\u2013", "-").replace("\u2014", "-").split())
    side_match = SIDE_RE.search(normalized)
    if not side_match:
        return None
    side_token = side_match.group(1).upper()
    side = "buy" if side_token.startswith("BUY") or side_token == "LONG" else "sell"
    symbol_match = SYMBOL_RE.search(normalized)
    symbol_token = symbol_match.group(1) if symbol_match else ""
    asset = _asset(symbol_token, chat_title)
    if not asset:
        return None

    order_kind = "market"
    if re.search(r"\b(?:BUY|SELL)\s+LIMIT\b", normalized, re.I):
        order_kind = "limit"
    elif re.search(r"\b(?:BUY|SELL)\s+STOP\b", normalized, re.I):
        order_kind = "stop"

    raw_range = RANGE_RE.search(normalized)
    raw_pre_side_entries = PRE_SIDE_ENTRIES_RE.search(normalized)
    raw_entry = ENTRY_RE.search(normalized)
    if raw_entry is None:
        raw_entry = SIDE_ENTRY_RE.search(normalized)
    if raw_entry is None:
        raw_entry = FOCUS_ENTRY_RE.search(normalized)
    if raw_entry is None:
        raw_entry = PRICE_SIDE_RE.search(normalized)
    if raw_range:
        entries = [_number(raw_range.group(1)), _number(raw_range.group(2))]
    elif raw_pre_side_entries:
        entries = [_number(value) for value in NUMBER_RE.findall(raw_pre_side_entries.group(1))]
    elif raw_entry:
        entries = [_number(raw_entry.group(1))]
    else:
        return None

    sl_match = SL_RE.search(normalized)
    raw_sl = _number(sl_match.group(1)) if sl_match else 0.0
    raw_tps = [_number(match.group(1)) for match in TP_RE.finditer(normalized)]
    if not raw_tps:
        return None

    full_levels = [value for value in [*entries, raw_sl, *raw_tps] if value > 0.0]
    lower, upper = _reference_scale(asset)
    anchors = [value for value in full_levels if lower <= value <= upper]
    anchor = min(anchors, key=lambda value: abs(value - entries[0])) if anchors else max(full_levels)
    entries = [_expand_level(value, anchor, asset) for value in entries]
    anchor = sum(entries) / len(entries)
    sl = _expand_level(raw_sl, anchor, asset) if raw_sl > 0.0 else 0.0
    tps = [_expand_level(value, anchor, asset) for value in raw_tps]

    entry_low, entry_high = min(entries), max(entries)
    if side == "buy" and sl >= entry_low:
        return None
    if side == "sell" and sl <= entry_high:
        return None
    ordered = _ordered_tps(side, anchor, tps)
    if sl <= 0.0 or not ordered:
        return None
    return GhpSignal(
        asset=asset,
        symbol_token=symbol_token.upper().replace(".S", ""),
        side=side,
        order_kind=order_kind,
        entry=float(entries[0]),
        entries=tuple(float(value) for value in entries),
        sl=float(sl),
        tps=ordered,
    )


def parse_ghp_message(text: str, chat_title: str = "") -> GhpMessage:
    normalized = " ".join(str(text or "").split())
    lower = normalized.lower()
    signal = _signal(normalized, chat_title)
    if signal is not None:
        return GhpMessage(kind="signal", signal=signal, asset=signal.asset, side=signal.side)

    symbol_match = SYMBOL_RE.search(normalized)
    asset = _asset(symbol_match.group(1) if symbol_match else "", chat_title)
    side_match = SIDE_RE.search(normalized)
    side = ""
    if side_match:
        side_token = side_match.group(1).upper()
        side = "buy" if side_token.startswith("BUY") or side_token == "LONG" else "sell"

    tp_hits = [int(value) for value in re.findall(r"\btp\s*(\d+)\s*(?:-|:)?\s*(?:hit|reached)\b", lower)]
    if tp_hits:
        return GhpMessage(kind="tp_hit", asset=asset, side=side, tp_level=max(tp_hits))
    if (
        re.search(r"\b(?:sl|stop\s*loss)\s+(?:was\s+)?(?:hit|touched)\b", lower)
        or re.search(r"\bhit\s+(?:the\s+)?(?:sl|stop\s*loss)\b", lower)
        or re.fullmatch(r"\s*sl\s*", lower)
    ):
        return GhpMessage(kind="sl_hit", asset=asset, side=side)
    if re.search(r"\b(?:cancel(?:\s+guys|\s+all)?|all\s+(?:buys|sells)\s+not\s+valid|not\s+valid)\b", lower):
        return GhpMessage(kind="cancel", asset=asset, side=side)
    if re.search(r"\b(?:close\s+half|half\s+partials?|profit\s+half\s+partials?)\b", lower):
        return GhpMessage(kind="close_partial", asset=asset, side=side, close_fraction=0.5, move_to_be="be" in lower or "breakeven" in lower)
    if re.search(
        r"\b(?:(?:move|set)\s+(?:the\s+)?(?:sl|stop\s*loss)\s+to|(?:sl|stop\s*loss)\s+to)\s+(?:be|breakeven)\b",
        lower,
    ) or re.search(r"\b(?:rest|remaining)(?:\s+(?:trades?|positions?))?\s+(?:to\s+)?be\b", lower):
        return GhpMessage(kind="breakeven", asset=asset, side=side, move_to_be=True)
    sl_update = SL_UPDATE_RE.search(normalized)
    if sl_update:
        return GhpMessage(kind="sl_update", asset=asset, side=side, stop_loss=_number(sl_update.group(1)))
    # A brand or a mention of take-profit is not an execution instruction.
    take_profit_command = re.fullmatch(
        r"take\s+profit(?:\s+now)?[.!\s]*", lower
    )
    if take_profit_command or re.search(r"\b(?:close\s+(?:now|full|fully|all|both|the\s+position|(?:buy|sell)\s+position)|i\s+closed\s+all)\b", lower):
        return GhpMessage(kind="close", asset=asset, side=side)
    if re.search(r"\bclose\s+(?:the\s+)?worst\s+entr(?:y|ies)\b", lower):
        return GhpMessage(kind="close_partial", asset=asset, side=side, close_fraction=0.0)
    if re.search(
        r"\b(?:add\s+position|buy\s+more|sell\s+more|big\s+buy|big\s+sell|re\s*entry|buy\s+again|sell\s+again|make\s+(?:one|1)\s+entry\s+now)\b",
        lower,
    ):
        return GhpMessage(kind="add", asset=asset, side=side)
    if re.search(r"\b(?:keep\s+(?:buying|selling)|we\s+(?:going|are\s+going)\s+to\s+(?:buy|sell)|make\s+ready\s+for\s+a\s+(?:buy|sell))\b", lower):
        return GhpMessage(kind="direction", asset=asset, side=side)
    if re.search(r"\b(?:buy|sell)\s+zone\b", lower) or re.search(r"\bi\s+will\s+(?:buy|sell)\b", lower):
        return GhpMessage(kind="pre_signal", asset=asset, side=side)
    if re.search(r"\b(?:hold|holding|still\s+running|trade\s+running|this\s+is\s+running)\b", lower):
        return GhpMessage(kind="hold", asset=asset, side=side)
    if re.search(r"\b(?:running\s+profit|pips\s+running|profit\s+start|moving\s+toward|breakeven\s+hit)\b", lower):
        return GhpMessage(kind="profit_update", asset=asset, side=side)
    return GhpMessage(kind="commentary", asset=asset, side=side)


def looks_like_ghp_signal(text: str, chat_title: str = "") -> bool:
    return parse_ghp_message(text, chat_title).kind == "signal"
