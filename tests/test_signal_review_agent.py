from app.signal_review_agent import review_signal, source_family
from app.telegram_signal_bot import ParsedSignal, _ghp_family_content_signature, _ghp_family_trade_signature


def _review(**overrides):
    payload = {
        "chat_id": -1001958009741,
        "chat_title": "GHP VIP-JACKPOT FX",
        "asset": "gold",
        "side": "sell",
        "order_kind": "market",
        "entries": [4415.0, 4416.0],
        "sl": 4430.0,
        "tps": [4400.0, 4385.0, 4365.0],
        "market_price": 4408.0,
        "raw_text": "SELL ENTRY 4415/16 SL 4430 TP 4400 TP 4385 TP 4365",
    }
    payload.update(overrides)
    return review_signal(**payload)


def test_ghp_gold_family_unifies_public_and_vip():
    assert source_family(-1002033681012, "Goldhunter") == "ghp_gold"
    assert source_family(-1001958009741, "GHP VIP") == "ghp_gold"


def test_ghp_mirror_signature_ignores_sl_correction():
    base = ParsedSignal(
        uid="public",
        chat_id=-1002033681012,
        chat_title="Goldhunter",
        post_author="",
        message_id=1,
        side="buy",
        asset="gold",
        entry=4355.92,
        entries=[4355.92],
        sl=4355.5,
        tp=4375.0,
        tps=[4375.0, 4400.0, 4433.0],
        order_type=0,
        order_kind="market",
        raw_text="",
    )
    corrected = ParsedSignal(**{**base.__dict__, "uid": "vip", "chat_id": -1001958009741, "sl": 4335.5})

    assert _ghp_family_content_signature(base) != _ghp_family_content_signature(corrected)
    assert _ghp_family_trade_signature(base) == _ghp_family_trade_signature(corrected)


def test_ghp_signal_after_tp1_waits_at_provider_entry_instead_of_chasing():
    result = _review(market_price=4397.0)
    assert result.decision == "accept"
    assert result.execution_mode == "provider_pending"
    assert "market_already_past_tp1_wait_for_retrace" in result.reasons


def test_rejects_mixed_buy_and_sell_context():
    result = _review(raw_text="SELL 4436 SL 4456 TP 4396 fight with me BUY 4410")
    assert result.decision == "reject"
    assert "mixed_direction_context" in result.reasons


def test_wait_for_execution_preserves_pending_entry():
    result = _review(
        chat_id=-1003495213392,
        chat_title="GHP CURRENCY",
        asset="eurusd",
        side="buy",
        entries=[1.1],
        sl=1.095,
        tps=[1.105],
        market_price=1.098,
        raw_text="EURUSD BUY 1.1000 SL 1.0950 TP1 1.1050 Wait for execution",
    )
    assert result.decision == "accept"
    assert result.execution_mode == "provider_pending"


def test_rejects_historically_negative_currency_asset():
    result = _review(
        chat_id=-1003495213392,
        chat_title="GHP CURRENCY",
        asset="usdcad",
        side="buy",
        entries=[1.38],
        sl=1.37,
        tps=[1.39],
        market_price=1.38,
        raw_text="USDCAD BUY 1.38 SL 1.37 TP1 1.39",
    )
    assert result.decision == "reject"
    assert "historically_negative_asset" in result.reasons
