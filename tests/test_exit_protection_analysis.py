from scripts.analyze_exit_protection import _metrics, _r


def test_r_multiple_respects_trade_direction() -> None:
    assert _r("buy", 100.0, 102.0, 2.0) == 1.0
    assert _r("sell", 100.0, 98.0, 2.0) == 1.0
    assert _r("buy", 100.0, 98.0, 2.0) == -1.0


def test_metrics_reports_profit_factor_and_drawdown() -> None:
    result = _metrics([1.0, -1.0, 2.0, -0.5])
    assert result["total_r"] == 1.5
    assert result["profit_factor"] == 2.0
    assert result["max_closed_drawdown_r"] == 1.0
