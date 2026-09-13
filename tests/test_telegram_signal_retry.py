from app.telegram_signal_bot import _is_retryable_market_data_error


def test_market_tick_failures_are_retryable():
    assert _is_retryable_market_data_error(RuntimeError("symbol_info_tick failed for XAUUSD+"))
    assert _is_retryable_market_data_error(RuntimeError("market data unavailable"))
    assert _is_retryable_market_data_error(RuntimeError("No tick for symbol"))


def test_order_rejections_are_not_market_data_retries():
    assert not _is_retryable_market_data_error(RuntimeError("order rejected retcode=10017"))
