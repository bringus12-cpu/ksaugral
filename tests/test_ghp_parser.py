from app.ghp_parser import parse_ghp_message


def test_parses_gold_market_and_short_targets() -> None:
    parsed = parse_ghp_message(
        "Gold sell now SL 4078.5 Entry 4071.27 Tp 69 Tp 67 Tp 65 Tp 63",
        "GHP VIP-JACKPOT FX",
    )
    assert parsed.kind == "signal"
    assert parsed.signal is not None
    assert parsed.signal.asset == "gold"
    assert parsed.signal.side == "sell"
    assert parsed.signal.tps == (4069.0, 4067.0, 4065.0, 4063.0)


def test_repairs_short_gold_entry() -> None:
    parsed = parse_ghp_message(
        "Gold sell limit Entry 48 sl 4063 Tp 4046 Tp 4043 Tp 4039 Tp 4036",
        "GHP VIP-JACKPOT FX",
    )
    assert parsed.signal is not None
    assert parsed.signal.entry == 4048.0
    assert parsed.signal.order_kind == "limit"


def test_repairs_btc_missing_zero() -> None:
    parsed = parse_ghp_message(
        "BTCUSD SELL NOW ENTRY 78450 SL 79240 Tp 78250 Tp 7800 Tp 77750 Tp 77500",
        "GHP VIP-JACKPOT INDICES & CRYPTO FX",
    )
    assert parsed.signal is not None
    assert 78000.0 in parsed.signal.tps


def test_parses_forex_decimal_signal() -> None:
    parsed = parse_ghp_message(
        "EURGBP SELL 0.85945 SL: 0.86100 Targets: Tp1 0.85760 Tp2 0.85180 Wait for execution",
        "GHP VIP-JACKPOT CURRENCY FX",
    )
    assert parsed.signal is not None
    assert parsed.signal.asset == "eurgbp"
    assert parsed.signal.entry == 0.85945
    assert parsed.signal.tps == (0.8576, 0.8518)


def test_classifies_management_messages() -> None:
    assert parse_ghp_message("Cancel Guys", "GHP VIP").kind == "cancel"
    assert parse_ghp_message("Move SL to breakeven", "GHP VIP").kind == "breakeven"
    partial = parse_ghp_message("Scalper can close half and set BE for zero risk!", "GHP VIP")
    assert partial.kind == "close_partial"
    assert partial.close_fraction == 0.5
    assert partial.move_to_be is True


def test_classifies_ghp_shorthand_management_messages() -> None:
    assert parse_ghp_message("Sl to be", "GHP VIP").kind == "breakeven"
    assert parse_ghp_message("Close BUY position", "GHP VIP").kind == "close"
    assert parse_ghp_message("Close worst entry", "GHP VIP").kind == "close_partial"
    assert parse_ghp_message("Sl", "GHP VIP").kind == "sl_hit"
    tp_update = parse_ghp_message("EURJPY - for holders TP1 - HIT TP2 - HIT", "GHP CURRENCY")
    assert tp_update.kind == "tp_hit"
    assert tp_update.tp_level == 2


def test_classifies_live_ghp_management_wording() -> None:
    assert parse_ghp_message("Hit SL", "GHP VIP").kind == "sl_hit"
    assert parse_ghp_message("Now move stoploss to breakeven", "GHP VIP").kind == "breakeven"
    assert parse_ghp_message("Buy again", "GHP VIP-JACKPOT FX").kind == "add"
    assert parse_ghp_message("make 1 entry now", "GHP VIP-JACKPOT FX").kind == "add"
    sl_update = parse_ghp_message("Make SL 4501", "GHP VIP-JACKPOT FX")
    assert sl_update.kind == "sl_update"
    assert sl_update.stop_loss == 4501.0


def test_fragmented_market_signal_parses_after_context_entry_is_added() -> None:
    parsed = parse_ghp_message(
        "Buy again\nSL 4370\nTP 4410 TP 4420 TP 4430\nENTRY 4395.25",
        "GHP VIP-JACKPOT FX",
    )
    assert parsed.kind == "signal"
    assert parsed.signal is not None
    assert parsed.signal.side == "buy"
    assert parsed.signal.entry == 4395.25
    assert parsed.signal.sl == 4370.0
    assert parsed.signal.tps == (4410.0, 4420.0, 4430.0)


def test_parses_all_supported_ghp_crosses() -> None:
    for symbol in ("EURCAD", "EURNZD", "EURCHF", "AUDCAD", "AUDNZD", "AUDCHF", "NZDCAD", "NZDCHF", "GBPAUD", "GBPCAD", "GBPNZD", "CADJPY", "CADCHF"):
        if symbol.endswith("JPY"):
            text = f"{symbol} BUY 150.00 SL 149.00 TP 151.00"
        else:
            text = f"{symbol} BUY 1.1000 SL 1.0900 TP 1.1100"
        parsed = parse_ghp_message(text, "GHP CURRENCY")
        assert parsed.signal is not None, symbol
        assert parsed.signal.asset == symbol.lower()


def test_parses_take_profit_and_entry_zone_format() -> None:
    parsed = parse_ghp_message(
        "ASSET: XAUUSD DIRECTION: BUY / LONG ENTRY ZONE: 4232.66 - 4233.00 "
        "STOP LOSS: 4222.66 TAKE PROFIT: 4315.00",
        "GOLDHUNTER PAUL WORLDWIDE COMMUNITY",
    )
    assert parsed.kind == "signal"
    assert parsed.signal is not None
    assert parsed.signal.entries == (4232.66, 4233.0)
    assert parsed.signal.tps == (4315.0,)


def test_parses_plural_sell_and_focus_entry() -> None:
    parsed = parse_ghp_message(
        "GOLD SELLS LIMIT Focus 4424 sell SL 4439 Targets 4414 Targets 4400 Targets 4385",
        "GOLDHUNTER PAUL WORLDWIDE COMMUNITY",
    )
    assert parsed.kind == "signal"
    assert parsed.signal is not None
    assert parsed.signal.side == "sell"
    assert parsed.signal.entry == 4424.0
    assert parsed.signal.tps == (4414.0, 4400.0, 4385.0)


def test_parses_price_before_sell_zone() -> None:
    parsed = parse_ghp_message(
        "4044 sell zone SL 4067 TP 4028 TP 4018 TP 4006 TP 3992",
        "GHP VIP-JACKPOT FX",
    )
    assert parsed.kind == "signal"
    assert parsed.signal is not None
    assert parsed.signal.entry == 4044.0


def test_parses_common_cross_pair_and_german_index() -> None:
    fx = parse_ghp_message("USDCAD SELL 1.3900 SL 1.3950 TP 1.3800", "GHP CURRENCY")
    index = parse_ghp_message("GER40.s SELL 25145 SL 25210 TP1 25080", "GHP INDICES")
    assert fx.signal is not None and fx.signal.asset == "usdcad"
    assert index.signal is not None and index.signal.asset == "ger40"


def test_parses_shorthand_layered_entries_before_side() -> None:
    parsed = parse_ghp_message(
        "4579/78/77 buy zone SL 4564 TP 4589 TP 4609 TP 4629",
        "GHP VIP-JACKPOT FX",
    )
    assert parsed.signal is not None
    assert parsed.signal.entries == (4579.0, 4578.0, 4577.0)


def test_parses_range_before_side() -> None:
    parsed = parse_ghp_message(
        "4629-4633 sell zone retest SL 4644 TP 4616 TP 4610 TP 4600",
        "GHP VIP-JACKPOT FX",
    )
    assert parsed.signal is not None
    assert parsed.signal.entries == (4629.0, 4633.0)
