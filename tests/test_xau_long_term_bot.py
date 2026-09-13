from __future__ import annotations

import os

import pytest

from app.xau_long_term_bot import DEPLOYABLE_STRATEGIES, EXCLUDED_STRATEGIES, _enabled_strategies


def test_default_strategy_set_excludes_rejected_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAU_LONG_TERM_STRATEGIES", raising=False)
    enabled = _enabled_strategies()
    assert enabled == DEPLOYABLE_STRATEGIES
    assert not set(enabled) & set(EXCLUDED_STRATEGIES)


def test_rejected_strategy_cannot_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAU_LONG_TERM_STRATEGIES", "time_series_momentum,donchian_breakout")
    with pytest.raises(ValueError, match="Explicitly excluded"):
        _enabled_strategies()
