from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AnalysisObjective:
    name: str
    ignore_drawdown: bool
    ignore_position_size: bool
    normalized_leg_lot: float
    win_rate_weight: float
    profit_weight: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_analysis_objective() -> AnalysisObjective:
    win_weight = max(0.0, float(os.getenv("STRATEGY_WIN_RATE_WEIGHT", "0.55")))
    profit_weight = max(0.0, float(os.getenv("STRATEGY_PROFIT_WEIGHT", "0.45")))
    total = win_weight + profit_weight
    if total <= 0:
        win_weight, profit_weight, total = 0.55, 0.45, 1.0
    return AnalysisObjective(
        name=os.getenv("STRATEGY_OPTIMIZATION_OBJECTIVE", "profit_win_rate").strip() or "profit_win_rate",
        ignore_drawdown=_env_bool("STRATEGY_ANALYSIS_IGNORE_DRAWDOWN", True),
        ignore_position_size=_env_bool("STRATEGY_ANALYSIS_IGNORE_POSITION_SIZE", True),
        normalized_leg_lot=max(0.01, float(os.getenv("STRATEGY_ANALYSIS_NORMALIZED_LEG_LOT", "0.01"))),
        win_rate_weight=win_weight / total,
        profit_weight=profit_weight / total,
    )


def rank_profit_win(
    rows: Iterable[dict[str, Any]],
    *,
    profit: Callable[[dict[str, Any]], float],
    win_rate: Callable[[dict[str, Any]], float],
    objective: AnalysisObjective | None = None,
) -> list[dict[str, Any]]:
    """Rank strategy edge independently of account size and drawdown."""
    objective = objective or load_analysis_objective()
    ranked = list(rows)
    if not ranked:
        return ranked

    profits = [float(profit(row)) for row in ranked]
    low, high = min(profits), max(profits)
    span = high - low
    for row, pnl in zip(ranked, profits):
        if high > 0.0:
            pnl_score = min(1.0, max(0.0, pnl / high))
        else:
            pnl_score = 0.5 if span <= 1e-12 else (pnl - low) / span
        rate_score = min(1.0, max(0.0, float(win_rate(row)) / 100.0))
        row["objective_score"] = round(
            100.0 * (objective.win_rate_weight * rate_score + objective.profit_weight * pnl_score),
            4,
        )

    return sorted(
        ranked,
        key=lambda row: (
            float(profit(row)) > 0.0,
            float(row["objective_score"]),
            float(win_rate(row)),
            float(profit(row)),
        ),
        reverse=True,
    )
