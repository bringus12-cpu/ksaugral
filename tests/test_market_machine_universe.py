from scripts.backtest_market_machine_universe import chunks
from scripts.build_mt5_symbol_universe import (
    canonical_key,
    classify,
    eligible,
    prioritize_core_symbols,
    round_robin,
)


class Info:
    def __init__(self, name: str, path: str, trade_mode: int = 1) -> None:
        self.name = name
        self.path = path
        self.description = ""
        self.currency_base = ""
        self.currency_profit = ""
        self.trade_mode = trade_mode


def test_chunks_preserve_every_symbol_once() -> None:
    result = chunks([str(index) for index in range(10)], 3)
    assert result == [["0", "1", "2"], ["3", "4", "5"], ["6", "7", "8"], ["9"]]


def test_classification_and_round_robin_are_diverse() -> None:
    assert classify(Info("XAUUSD", "Metals")) == "metals"
    assert classify(Info("AAPL", "Stocks\\US")) == "stocks"
    grouped = {"forex": [Info("EURUSD", "Forex")], "stocks": [Info("AAPL", "Stocks\\US")]}
    assert [item.name for item in round_robin(grouped)[:2]] == ["EURUSD", "AAPL"]


def test_disabled_symbol_is_rejected() -> None:
    assert eligible(Info("EURUSD", "Forex", trade_mode=0)) is False


def test_stock_and_future_aliases_are_deduplicated() -> None:
    assert canonical_key(Info("AAPL", "Stocks\\US")) == canonical_key(Info("AAPL.24H", "Stocks\\US.24H"))
    assert canonical_key(Info("DJ30", "CFDs\\Indices")) == canonical_key(Info("DJ30ft", "CFDs\\Indices"))


def test_core_symbols_are_prioritized_without_losing_the_diverse_tail() -> None:
    items = [
        Info("EURUSD+", "Forex"),
        Info("AAPL", "Stocks\\US"),
        Info("XAUUSD+", "Metals"),
        Info("NAS100", "CFDs\\Indices"),
    ]
    assert [item.name for item in prioritize_core_symbols(items)] == [
        "XAUUSD+",
        "NAS100",
        "EURUSD+",
        "AAPL",
    ]
