from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import pytest

from app.agent_teams import (
    TeamSpec,
    _agent_votes,
    _candidate_votes,
    _candidate_is_eligible,
    _control_review,
    _default_control_state,
    _default_learning_state,
    _long_term_votes,
    _open_capacity_available,
    _position_volume,
    _publish_control_message,
    _score_instrument_frame,
    _scout_agent_pack,
    _scout_key,
    _update_learning,
    supervise,
)
from app.indicators import enrich


def _vote(agent: str, side: str, confidence: float) -> dict:
    return {"agent": agent, "side": side, "confidence": confidence}


def test_supervisor_accepts_clear_consensus():
    votes = [
        _vote("trend", "buy", 0.9),
        _vote("momentum", "buy", 0.8),
        _vote("structure", "buy", 0.8),
        _vote("price_action", "buy", 0.7),
        _vote("volatility", "hold", 0.6),
        _vote("mean_reversion", "hold", 0.4),
    ]
    result = supervise(votes, threshold=0.50, min_directional_votes=3)
    assert result["decision"] == "buy"
    assert result["confidence"] >= 0.50


def test_supervisor_rejects_split_vote():
    votes = [
        _vote("trend", "buy", 0.8),
        _vote("momentum", "sell", 0.8),
        _vote("structure", "buy", 0.7),
        _vote("price_action", "sell", 0.7),
        _vote("volatility", "hold", 0.8),
        _vote("mean_reversion", "hold", 0.6),
    ]
    result = supervise(votes, threshold=0.50, min_directional_votes=3)
    assert result["decision"] == "hold"


def test_supervisor_ignores_shadow_candidate():
    votes = [
        _vote("trend", "buy", 0.9),
        _vote("momentum", "buy", 0.9),
        _vote("structure", "buy", 0.9),
        {"agent": "breakout_specialist", "side": "sell", "confidence": 1.0, "status": "shadow"},
    ]
    result = supervise(votes, threshold=0.45, min_directional_votes=3)
    assert result["decision"] == "buy"
    assert result["counts"]["sell"] == 0


def test_mean_reversion_keeps_computed_confidence():
    m1 = pd.DataFrame(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "atr14": 1.0,
                "ema20": 100.0,
                "ema50": 100.0,
                "rsi14": 50.0,
                "bb_lower": 99.0,
                "bb_upper": 101.0,
            }
            for _ in range(20)
        ]
    )
    m1.loc[len(m1) - 2, ["open", "high", "low", "close", "rsi14"]] = [99.0, 99.2, 97.5, 98.0, 20.0]
    m5 = pd.DataFrame(
        [{"close": 100.0, "atr14": 2.0, "ema20": 100.0, "ema50": 100.0, "adx14": 15.0}] * 3
    )
    m15 = pd.DataFrame([{"ema20": 100.0, "ema50": 100.0, "adx14": 15.0}] * 3)

    votes = _agent_votes(m1, m5, m15, spread=0.05)
    reversion = next(vote for vote in votes if vote["agent"] == "mean_reversion")

    assert reversion["side"] == "buy"
    assert reversion["confidence"] > 0.80


def test_candidate_registry_exposes_all_portfolio_specialists():
    start = pd.Timestamp("2026-01-01", tz="UTC")

    def frame(step: str) -> pd.DataFrame:
        rows = []
        price = 100.0
        for index in range(280):
            price += 0.03 + (0.02 if index % 7 else -0.01)
            rows.append(
                {
                    "time": start + pd.Timedelta(step) * index,
                    "open": price - 0.02,
                    "high": price + 0.08,
                    "low": price - 0.08,
                    "close": price,
                    "tick_volume": 100 + index % 20,
                }
            )
        return enrich(pd.DataFrame(rows))

    votes = _candidate_votes(frame("1min"), frame("5min"), frame("15min"), _default_learning_state())
    names = {vote["agent"] for vote in votes}

    assert {
        "breakout_specialist",
        "pullback_specialist",
        "liquidity_specialist",
        "multi_tf_specialist",
        "bollinger_rsi_specialist",
        "macd_ema_specialist",
        "dual_thrust_specialist",
        "squeeze_release_specialist",
        "vwap_reclaim_specialist",
    } <= names


def test_meta_learner_promotes_consistent_candidate():
    learning = _default_learning_state()
    votes = [{"agent": "breakout_specialist", "side": "buy", "confidence": 0.8, "status": "shadow"}]
    with TemporaryDirectory() as temp_dir:
        events_path = Path(temp_dir) / "events.jsonl"
        for _ in range(12):
            _update_learning(learning, votes, "buy", 1.0, events_path)
    record = learning["agents"]["breakout_specialist"]
    assert record["status"] == "active"
    assert record["observations"] == 12
    assert learning["promotions"]


def test_meta_learner_keeps_instrument_models_isolated():
    learning = _default_learning_state()
    votes = [{"agent": "squeeze_release_specialist", "side": "buy", "confidence": 0.8, "status": "shadow"}]
    with TemporaryDirectory() as temp_dir:
        events_path = Path(temp_dir) / "events.jsonl"
        for _ in range(12):
            _update_learning(learning, votes, "buy", 1.0, events_path, "xau")

    assert learning["team_agents"]["xau"]["squeeze_release_specialist"]["status"] == "active"
    assert "nasdaq" not in learning["team_agents"]
    assert learning["agents"]["squeeze_release_specialist"]["status"] == "shadow"


def test_candidate_allowlist_is_scoped_by_team(monkeypatch):
    monkeypatch.setenv(
        "AGENT_TEAM_CANDIDATE_ALLOWLIST_JSON",
        '{"xau":["squeeze_release_specialist"],"nasdaq":["macd_ema_specialist"]}',
    )

    assert _candidate_is_eligible("xau", "squeeze_release_specialist")
    assert not _candidate_is_eligible("xau", "macd_ema_specialist")
    assert _candidate_is_eligible("nasdaq", "macd_ema_specialist")


def test_instrument_scout_scores_clean_liquid_series():
    rows = []
    price = 100.0
    for index in range(180):
        price += 0.08
        rows.append(
            {
                "open": price - 0.05,
                "high": price + 0.12,
                "low": price - 0.12,
                "close": price,
                "tick_volume": 100 + index,
            }
        )
    result = _score_instrument_frame(pd.DataFrame(rows), spread=0.02, tick_age_seconds=10)
    assert result["score"] >= 68
    assert result["spread_atr"] < 0.2
    assert result["efficiency"] > 0.5


def test_instrument_scout_rejects_short_history():
    result = _score_instrument_frame(pd.DataFrame([{"open": 1, "high": 2, "low": 0, "close": 1}]), 0.1, 0)
    assert result["score"] == 0


def test_instrument_scout_rejects_stale_tick():
    frame = pd.DataFrame([{"open": 1, "high": 2, "low": 0, "close": 1}] * 140)
    result = _score_instrument_frame(frame, spread=0.1, tick_age_seconds=259201)
    assert result["score"] == 0
    assert "72h" in result["reason"]


def test_instrument_scout_builds_stable_team_key_and_agent_pack():
    assert _scout_key("BTCUSD+") == "scout_btcusd"
    agents = _scout_agent_pack({"efficiency": 0.4, "atr_pct": 0.003, "spread_atr": 0.05})
    assert "breakout_specialist" in agents
    assert "multi_tf_specialist" in agents
    assert "liquidity_specialist" in agents


def test_control_team_approves_quality_proposal():
    team = TeamSpec("test", "Test Team", "TEST", "TEST", symbol="TEST")
    result = _control_review(
        team,
        {"decision": "buy", "confidence": 0.72, "margin": 0.30},
        {"trades": 8, "win_rate": 62.5, "pnl": 10.0},
        _default_control_state(),
    )
    assert result["approved"]
    assert result["verdict"] == "approved"


def test_control_team_blocks_weak_proposal_with_bad_history():
    team = TeamSpec("test", "Test Team", "TEST", "TEST", symbol="TEST")
    result = _control_review(
        team,
        {"decision": "sell", "confidence": 0.50, "margin": 0.10},
        {"trades": 10, "win_rate": 20.0, "pnl": -50.0},
        _default_control_state(),
    )
    assert not result["approved"]
    assert result["verdict"] == "blocked"


def test_control_team_detects_opposite_team_message():
    control = _default_control_state()
    _publish_control_message(
        control,
        "other_manager",
        "team_manager",
        "trade_proposal",
        "Other proposes SELL",
        "other",
        "TEST",
        {"side": "sell", "confidence": 0.8},
    )
    team = TeamSpec("test", "Test Team", "TEST", "TEST", symbol="TEST")
    result = _control_review(
        team,
        {"decision": "buy", "confidence": 0.60, "margin": 0.18},
        {"trades": 0, "win_rate": 0.0, "pnl": 0.0},
        control,
    )
    conflict_vote = next(vote for vote in result["votes"] if vote["manager"] == "conflict_controller")
    assert not conflict_vote["approve"]


def test_unlimited_open_capacity_never_blocks():
    assert _open_capacity_available(0, 0)
    assert _open_capacity_available(10000, 0)


def test_bounded_open_capacity_still_works():
    assert _open_capacity_available(2, 3)
    assert not _open_capacity_available(3, 3)


def test_dynamic_risk_volume_scales_with_equity(monkeypatch):
    monkeypatch.setenv("AGENT_TEAM_LOT_MODE", "dynamic_risk")
    monkeypatch.setenv("AGENT_TEAM_RISK_PER_TRADE_PCT", "0.10")
    monkeypatch.setenv("AGENT_TEAM_DYNAMIC_MAX_LOT", "1.0")
    monkeypatch.setattr("app.agent_teams.calc_loss_per_lot", lambda *args: 100.0)
    monkeypatch.setattr("app.agent_teams.normalize_volume", lambda symbol, volume, minimum, maximum: min(maximum, max(minimum, volume)))
    team = TeamSpec("test", "Test", "TEST", "TEST", symbol="TEST", min_volume=0.01, requested_volume=0.01)

    small, _ = _position_volume(team, "buy", 100.0, 99.0, 1000.0, {})
    large, _ = _position_volume(team, "buy", 100.0, 99.0, 5000.0, {})

    assert small == 0.01
    assert large == 0.05


def test_equity_step_volume_starts_at_point_one_and_adds_each_300(monkeypatch):
    monkeypatch.setenv("AGENT_TEAM_LOT_MODE", "equity_step")
    monkeypatch.setenv("AGENT_TEAM_DYNAMIC_MAX_LOT", "100.0")
    monkeypatch.setenv("AGENT_TEAM_EQUITY_BASE_USD", "1000")
    monkeypatch.setenv("AGENT_TEAM_EQUITY_BASE_LOT", "0.10")
    monkeypatch.setenv("AGENT_TEAM_EQUITY_STEP_USD", "300")
    monkeypatch.setenv("AGENT_TEAM_EQUITY_STEP_LOT", "0.01")
    monkeypatch.setattr("app.agent_teams.calc_loss_per_lot", lambda *args: 100.0)
    monkeypatch.setattr(
        "app.agent_teams.normalize_volume",
        lambda symbol, volume, minimum, maximum: min(maximum, max(minimum, volume)),
    )
    team = TeamSpec("test", "Test", "TEST", "TEST", symbol="TEST", min_volume=0.01, requested_volume=0.01)

    below_base, below_meta = _position_volume(team, "buy", 100.0, 99.0, 900.0, {})
    base, base_meta = _position_volume(team, "buy", 100.0, 99.0, 1000.0, {})
    before_step, _ = _position_volume(team, "buy", 100.0, 99.0, 1299.99, {})
    first_step, first_meta = _position_volume(team, "buy", 100.0, 99.0, 1300.0, {})
    third_step, third_meta = _position_volume(team, "buy", 100.0, 99.0, 1900.0, {})

    assert below_base == pytest.approx(0.10)
    assert base == pytest.approx(0.10)
    assert before_step == pytest.approx(0.10)
    assert first_step == pytest.approx(0.11)
    assert third_step == pytest.approx(0.13)
    assert below_meta["equity_steps"] == 0
    assert base_meta["equity_steps"] == 0
    assert first_meta["equity_steps"] == 1
    assert third_meta["equity_steps"] == 3


def test_martingale_is_bounded_and_disabled_by_default(monkeypatch):
    monkeypatch.setenv("AGENT_TEAM_LOT_MODE", "dynamic_risk")
    monkeypatch.setenv("AGENT_TEAM_RISK_PER_TRADE_PCT", "0.10")
    monkeypatch.setenv("AGENT_TEAM_DYNAMIC_MAX_LOT", "1.0")
    monkeypatch.setattr("app.agent_teams.calc_loss_per_lot", lambda *args: 100.0)
    monkeypatch.setattr("app.agent_teams.normalize_volume", lambda symbol, volume, minimum, maximum: min(maximum, max(minimum, volume)))
    team = TeamSpec("test", "Test", "TEST", "TEST", symbol="TEST", min_volume=0.01, requested_volume=0.01)
    runtime = {"loss_streaks": {"test": 3}}

    plain, plain_meta = _position_volume(team, "buy", 100.0, 99.0, 5000.0, runtime)
    monkeypatch.setenv("AGENT_TEAM_MARTINGALE_ENABLED", "true")
    monkeypatch.setenv("AGENT_TEAM_MARTINGALE_FACTOR", "2.0")
    monkeypatch.setenv("AGENT_TEAM_MARTINGALE_MAX_STEPS", "1")
    monkeypatch.setenv("AGENT_TEAM_MARTINGALE_MAX_MULTIPLIER", "1.5")
    bounded, bounded_meta = _position_volume(team, "buy", 100.0, 99.0, 5000.0, runtime)

    assert plain == 0.05
    assert not plain_meta["martingale_enabled"]
    assert bounded == pytest.approx(0.075)
    assert bounded_meta["martingale_multiplier"] == 1.5


def test_long_term_brigade_finds_aligned_uptrend():
    h1 = pd.DataFrame(
        [
            {"close": 100, "atr14": 1.0},
            {"close": 101, "atr14": 1.0},
            {"close": 102, "atr14": 1.0},
        ]
    )
    h4 = pd.DataFrame(
        [
            {"close": 100, "atr14": 3.0, "ema20": 104, "ema50": 100, "adx14": 28, "rsi14": 60, "hh20": 102, "ll20": 90},
            {"close": 106, "atr14": 3.0, "ema20": 105, "ema50": 101, "adx14": 30, "rsi14": 62, "hh20": 104, "ll20": 91},
            {"close": 108, "atr14": 3.0, "ema20": 106, "ema50": 102, "adx14": 31, "rsi14": 63, "hh20": 106, "ll20": 92},
        ]
    )
    d1 = pd.DataFrame(
        [
            {"ema20": 108, "ema50": 100, "ema200": 90},
            {"ema20": 110, "ema50": 101, "ema200": 91},
            {"ema20": 112, "ema50": 102, "ema200": 92},
        ]
    )
    votes = _long_term_votes(h1, h4, d1, spread=0.05)
    assert sum(1 for vote in votes if vote["side"] == "buy") >= 4
