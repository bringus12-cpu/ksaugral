from datetime import UTC, datetime, timedelta

from scripts.merge_selected_channels_portfolio import _is_mirror
from scripts.optimize_funded_multi_leg_30d import _valid, _simulate_leg
from types import SimpleNamespace
import pandas as pd


def event(module="newtg:a", offset=0, symbol="XAUUSD"):
    return dict(module=module, opened=datetime(2026, 7, 1, tzinfo=UTC) + timedelta(minutes=offset),
                symbol=symbol, side="buy", target_index=1, entry=4000.0, sl=3990.0)


def test_future_signal_cannot_suppress_current_signal():
    assert not _is_mirror(event(), [event("newtg:b", 1)])


def test_same_channel_legs_are_preserved():
    assert not _is_mirror(event(), [event(offset=-1)])


def test_prior_cross_channel_broker_alias_matches():
    assert _is_mirror(event(), [event("ghp:source", -1, "XAUUSD+")])


def test_different_contract_and_expired_match_are_preserved():
    assert not _is_mirror(event(), [event("newtg:b", -1, "XAUUSD247")])
    assert not _is_mirror(event(), [event("newtg:b", -21)])


def test_different_targets_are_not_duplicates():
    other = {**event("newtg:b", -1), "target_index": 2}
    assert not _is_mirror(event(), [other])


def test_zero_or_nonfinite_stop_is_not_a_valid_buy():
    assert not _valid("buy", 4000, 0, 4005)
    assert not _valid("buy", 4000, float("nan"), 4005)


def test_pending_uses_available_open_not_future_close():
    bars = pd.DataFrame({"time": pd.to_datetime(["2026-07-01T10:00Z", "2026-07-01T10:01Z"]),
                         "high": [4002, 4005], "low": [3998, 4000], "close": [3998, 4004]})
    signal = SimpleNamespace(side="buy", sl=3990, tps=[4004])
    result = _simulate_leg(signal, "XAUUSD", bars, 0, 3999, True, 1, "none", 0, 15, 1,
                           initial_market_price=4001)
    assert result["status"] == "win"
    assert result["entry_idx"] == 0
