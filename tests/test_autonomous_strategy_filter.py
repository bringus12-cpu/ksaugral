from __future__ import annotations

import pytest

from app.strategies import AUTONOMOUS_STRATEGIES, _enabled_autonomous_strategies


def test_autonomous_strategies_default_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTONOMOUS_ENABLED_STRATEGIES", raising=False)
    assert _enabled_autonomous_strategies() == set(AUTONOMOUS_STRATEGIES)


def test_autonomous_strategies_can_be_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED_STRATEGIES", "trend_pullback,breakout_momentum")
    assert _enabled_autonomous_strategies() == {"trend_pullback", "breakout_momentum"}


def test_autonomous_strategies_reject_unknown_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED_STRATEGIES", "trend_pullback,unknown")
    with pytest.raises(ValueError, match="unknown"):
        _enabled_autonomous_strategies()
