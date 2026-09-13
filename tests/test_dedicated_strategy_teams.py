import json

from app.agent_teams import (
    DEDICATED_STRATEGY_TEAM_SPECS,
    _configured_dedicated_strategy_team_specs,
    _dedicated_strategy_levels,
    _dedicated_strategy_parameters,
)


def test_dedicated_pairs_are_registered() -> None:
    by_key = {item[0]: item for item in DEDICATED_STRATEGY_TEAM_SPECS}
    assert by_key["nas100_dual_thrust"][2:] == (
        "NAS100",
        "NAS100 DUAL THRUST",
        "dedicated:dual_thrust_specialist",
    )
    assert by_key["dj30_roc"][2:] == (
        "DJ30",
        "DJ30 ROC",
        "dedicated:roc_acceleration_specialist",
    )


def test_dedicated_level_helper_is_available() -> None:
    assert callable(_dedicated_strategy_levels)


def test_dedicated_parameters_match_backtest_horizons() -> None:
    assert _dedicated_strategy_parameters("mean_reversion") == (1.0, 1.25, 48)
    assert _dedicated_strategy_parameters("dual_thrust_specialist") == (1.3, 2.0, 72)
    assert _dedicated_strategy_parameters("ichimoku_specialist") == (1.2, 1.8, 72)


def test_configured_dedicated_pairs_are_validated(monkeypatch) -> None:
    monkeypatch.setenv(
        "AGENT_TEAM_DEDICATED_PAIRS_JSON",
        json.dumps(
            [
                {"key": "MM-NAS", "symbol": "NAS100", "strategy": "roc_acceleration_specialist"},
                {"key": "MM-NUGT", "symbol": "NUGT", "strategy": "mean_reversion"},
                {"key": "bad", "symbol": "XAUUSD", "strategy": "not_a_strategy"},
            ]
        ),
    )
    assert _configured_dedicated_strategy_team_specs() == [
        (
            "mm_nas",
            "NAS100 roc_acceleration_specialist",
            "NAS100",
            "MM NAS100 roc_acceleration_spec",
            "dedicated:roc_acceleration_specialist",
        ),
        (
            "mm_nugt",
            "NUGT mean_reversion",
            "NUGT",
            "MM NUGT mean_reversion",
            "dedicated:mean_reversion",
        ),
    ]
