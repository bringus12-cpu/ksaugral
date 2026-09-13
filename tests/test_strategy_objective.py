from __future__ import annotations

from app.strategy_objective import AnalysisObjective, rank_profit_win


OBJECTIVE = AnalysisObjective(
    name="profit_win_rate",
    ignore_drawdown=True,
    ignore_position_size=True,
    normalized_leg_lot=0.01,
    win_rate_weight=0.55,
    profit_weight=0.45,
)


def test_ranking_combines_profit_and_win_rate() -> None:
    rows = [
        {"name": "high_profit", "profit": 100.0, "win_rate": 60.0, "dd": -900.0},
        {"name": "balanced", "profit": 80.0, "win_rate": 80.0, "dd": -50.0},
    ]

    ranked = rank_profit_win(
        rows,
        profit=lambda row: row["profit"],
        win_rate=lambda row: row["win_rate"],
        objective=OBJECTIVE,
    )

    assert ranked[0]["name"] == "balanced"


def test_ranking_ignores_drawdown() -> None:
    rows = [
        {"name": "same_edge_large_dd", "profit": 50.0, "win_rate": 75.0, "dd": -1000.0},
        {"name": "same_edge_small_dd", "profit": 50.0, "win_rate": 75.0, "dd": -10.0},
    ]

    ranked = rank_profit_win(
        rows,
        profit=lambda row: row["profit"],
        win_rate=lambda row: row["win_rate"],
        objective=OBJECTIVE,
    )

    assert ranked[0]["objective_score"] == ranked[1]["objective_score"]


def test_profitable_strategy_ranks_before_loss_even_with_lower_win_rate() -> None:
    rows = [
        {"name": "loss", "profit": -1.0, "win_rate": 95.0},
        {"name": "profit", "profit": 1.0, "win_rate": 40.0},
    ]

    ranked = rank_profit_win(
        rows,
        profit=lambda row: row["profit"],
        win_rate=lambda row: row["win_rate"],
        objective=OBJECTIVE,
    )

    assert ranked[0]["name"] == "profit"
