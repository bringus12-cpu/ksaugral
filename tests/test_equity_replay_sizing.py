import pytest

from scripts.replay_available_equity_60 import sized_lot


def test_risk_is_based_on_current_equity():
    assert sized_lot(1000, 500, .01, .01, 100) == .03
    assert sized_lot(800, 500, .01, .01, 100) == .02


def test_does_not_raise_volume_to_minimum():
    assert sized_lot(100, 500, .01, .01, 100) == 0


@pytest.mark.parametrize('equity', [0, -100])
def test_nonpositive_equity_cannot_open(equity):
    assert sized_lot(equity, 500, .01, .01, 100) == 0


def test_respects_broker_maximum_and_rounds_down():
    assert sized_lot(100000, 500, .01, .01, .5) == .5
    lot = sized_lot(1234, 713, .01, .01, 100)
    assert lot * 713 <= 1234 * .0175
