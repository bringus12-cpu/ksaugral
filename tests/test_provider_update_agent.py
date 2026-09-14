from app.provider_update_agent import pending_cancel_candidate, review_provider_pending_update


def _review(text: str, **overrides):
    values = {
        "text": text,
        "scoped": True,
        "side": "buy",
        "asset": "gold",
        "entry": 4333.5,
        "tp1": 4344.0,
        "current_price": 4330.0,
        "created_utc": "",
    }
    values.update(overrides)
    return review_provider_pending_update(**values)


def test_detects_explicit_and_stale_ghp_cancel() -> None:
    result = _review("Not valid anymore already done in the night")
    assert result.decision == "cancel"
    assert result.confidence == 100
    assert "provider_declared_signal_stale" in result.reasons


def test_detects_multilingual_cancel_messages() -> None:
    messages = (
        "Cancel pending now",
        "Anuluj zlecenie",
        "Senal invalida",
        "Sinal invalido",
        "Signal ungultig",
        "Signal invalide",
        "Signaal ongeldig",
    )
    assert all(pending_cancel_candidate(message) for message in messages)
    assert all(_review(message).decision == "cancel" for message in messages)


def test_negated_cancel_keeps_pending() -> None:
    assert not pending_cancel_candidate("Do not cancel, order remains valid")
    result = _review("Do not cancel, order remains valid")
    assert result.decision == "keep"
    assert result.reasons == ("explicit_keep_instruction",)


def test_conditional_cancel_does_not_fire_early() -> None:
    result = _review("Cancel pending if price reaches 4344")
    assert result.decision == "keep"
    assert result.reasons == ("conditional_instruction_not_active",)


def test_side_or_asset_mismatch_keeps_pending() -> None:
    assert _review("Cancel SELL setup").decision == "keep"
    assert _review("Cancel NASDAQ setup").decision == "keep"


def test_market_context_is_recorded_but_does_not_override_provider() -> None:
    result = _review("Cancel", current_price=4345.0)
    assert result.decision == "cancel"
    assert "market_already_reached_tp1" in result.reasons
    assert "market_context_checked" in result.reasons


def test_unscoped_cancel_is_ignored() -> None:
    result = _review("Cancel", scoped=False)
    assert result.decision == "ignore"
    assert result.reasons == ("update_not_scoped_to_live_signal",)
