from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class ProviderPendingReview:
    decision: str
    intent: str
    confidence: int
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plain(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.lower().replace("’", "'").split())


KEEP_RE = re.compile(
    r"\b(?:do\s+not|don't|dont)\s+(?:cancel|delete|remove)\b|"
    r"\b(?:keep|leave)\s+(?:the\s+)?(?:order|pending|setup|signal)\b|"
    r"\b(?:still|remains?)\s+valid\b|"
    r"\bnie\s+(?:anuluj|usun|kasuj)|\b(?:zostaw|trzymaj)\s+(?:pending|zlecen)|"
    r"\bnadal\s+wazn|"
    r"\bno\s+(?:cancelar|eliminar)|\bnao\s+(?:cancelar|remover)|"
    r"\bnicht\s+(?:stornieren|loschen)|\bne\s+pas\s+(?:annuler|supprimer)|"
    r"\bniet\s+(?:annuleren|verwijderen)",
    re.I,
)

CANCEL_RE = re.compile(
    r"\b(?:cancel|cancelled|canceled|delete|remove|drop)\b|"
    r"\b(?:setup|signal|order|pending)\s+(?:cancelled|canceled|invalid|expired)\b|"
    r"\b(?:not\s+valid|no\s+longer\s+valid|do\s+not\s+enter|don't\s+enter|dont\s+enter|skip\s+(?:this\s+)?trade)\b|"
    r"\b(?:anuluj|anulowane|anulowany|usun|skasuj|pomin|nie\s+wchodz|niewazn)\w*\b|"
    r"\b(?:cancelar|cancela|cancelado|eliminar|elimina|no\s+entrar|senal\s+invalida)\b|"
    r"\b(?:cancele|cancelado|remover|remova|nao\s+entrar|sinal\s+invalido)\b|"
    r"\b(?:stornieren|storniert|loschen|nicht\s+einsteigen|signal\s+ungultig)\b|"
    r"\b(?:annuler|annule|supprimer|ne\s+pas\s+entrer|signal\s+invalide)\b|"
    r"\b(?:annuleren|geannuleerd|verwijderen|niet\s+instappen|signaal\s+ongeldig)\b",
    re.I,
)

STALE_RE = re.compile(
    r"\b(?:too\s+late|missed|already\s+done|outdated|expired|old\s+signal|"
    r"za\s+pozno|nieaktualn|wygasl|juz\s+zrobion|"
    r"demasiado\s+tarde|caducad|expirad|veraltet|abgelaufen|trop\s+tard|expire)\w*\b",
    re.I,
)

CONDITIONAL_RE = re.compile(
    r"\b(?:if|when|unless|jesli|jezeli|gdy|si|cuando|se|quando|wenn|falls|si|lorsque)\b",
    re.I,
)

SIDE_RE = re.compile(r"\b(buy|long|sell|short|kup|sprzedaj|compra|venta)\b", re.I)
ASSET_PATTERNS = {
    "gold": re.compile(r"\b(?:gold|xauusd|xau)\b", re.I),
    "nas100": re.compile(r"\b(?:nas100|nasdaq|us100|ustec|ndaq)\b", re.I),
    "us30": re.compile(r"\b(?:us30|dj30|dow)\b", re.I),
    "btc": re.compile(r"\b(?:btc|btcusd|bitcoin)\b", re.I),
}


def pending_cancel_candidate(text: str) -> bool:
    value = _plain(text)
    return bool(value and not KEEP_RE.search(value) and CANCEL_RE.search(value))


def _referenced_side(value: str) -> str:
    match = SIDE_RE.search(value)
    if not match:
        return ""
    token = match.group(1).lower()
    return "buy" if token in {"buy", "long", "kup", "compra"} else "sell"


def _referenced_asset(value: str) -> str:
    for asset, pattern in ASSET_PATTERNS.items():
        if pattern.search(value):
            return asset
    return ""


def review_provider_pending_update(
    *,
    text: str,
    scoped: bool,
    side: str = "",
    asset: str = "",
    entry: float = 0.0,
    tp1: float = 0.0,
    current_price: float = 0.0,
    created_utc: str = "",
) -> ProviderPendingReview:
    value = _plain(text)
    if not value:
        return ProviderPendingReview("ignore", "none", 0, ("empty_message",))
    if KEEP_RE.search(value):
        return ProviderPendingReview("keep", "keep", 100, ("explicit_keep_instruction",))
    if not CANCEL_RE.search(value):
        return ProviderPendingReview("ignore", "none", 0, ("no_cancel_intent",))
    if CONDITIONAL_RE.search(value) and not re.search(r"\b(?:now|teraz|ahora|agora|jetzt|maintenant|nu)\b", value):
        return ProviderPendingReview("keep", "conditional", 85, ("conditional_instruction_not_active",))
    if not scoped:
        return ProviderPendingReview("ignore", "cancel", 30, ("update_not_scoped_to_live_signal",))

    message_side = _referenced_side(value)
    if message_side and side and message_side != str(side).lower():
        return ProviderPendingReview("keep", "cancel", 95, ("referenced_side_mismatch",))
    message_asset = _referenced_asset(value)
    if message_asset and asset and message_asset != str(asset).lower():
        return ProviderPendingReview("keep", "cancel", 95, ("referenced_asset_mismatch",))

    reasons = ["explicit_provider_cancel"]
    confidence = 95
    if STALE_RE.search(value):
        reasons.append("provider_declared_signal_stale")
        confidence = 100
    if current_price > 0.0 and tp1 > 0.0 and side in {"buy", "sell"}:
        reached = current_price >= tp1 if side == "buy" else current_price <= tp1
        if reached:
            reasons.append("market_already_reached_tp1")
    if entry > 0.0 and current_price > 0.0:
        reasons.append("market_context_checked")
    if created_utc:
        try:
            created = datetime.fromisoformat(str(created_utc).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_minutes = max(0.0, (datetime.now(UTC) - created.astimezone(UTC)).total_seconds() / 60.0)
            if age_minutes >= 15.0:
                reasons.append("pending_older_than_15_minutes")
        except (TypeError, ValueError):
            pass
    return ProviderPendingReview("cancel", "cancel", confidence, tuple(reasons))
