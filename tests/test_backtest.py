import pytest

from backend.backtest import run_backtest
from backend.indicators import analyze, adx


def rising_candles(count=100):
    candles = []
    for i in range(count):
        close = 100 + i * 0.5
        candles.append([i * 3600000, close - 0.2, close + 1.0, close - 1.0, close, 100.0])
    return candles


def test_adx_and_quality_filters_are_reported():
    metrics = analyze(rising_candles(80))
    assert metrics["adx14"] >= 20
    assert metrics["volume_ratio"] == pytest.approx(1.0)
    assert metrics["signal"] == "LONG"


def test_backtest_enters_next_bar_and_reports_cost_assumptions():
    result = run_backtest(rising_candles(120))
    assert result["trade_count"] > 0
    assert result["assumptions"]["entry"] == "next candle open"
    assert result["assumptions"]["same_candle_stop_and_target"] == "stop first"
    assert result["assumptions"]["funding"] == "not modeled"
    assert all("r_multiple" in trade and "costs" in trade for trade in result["trades"])


def test_backtest_requires_enough_history():
    with pytest.raises(ValueError, match="At least 52 candles"):
        run_backtest(rising_candles(40))
