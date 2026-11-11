import pytest

from backend import backtest


def _flat_candles(count=60):
    return [
        [index * 60_000, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for index in range(count)
    ]


def test_backtest_drawdown_includes_adverse_intrabar_equity(monkeypatch):
    candles = _flat_candles()
    # Entry is on candle 50. Candle 51 trades close to, but not through,
    # either exit level. Its high occurs before its low in the conservative
    # mark-to-market envelope, while its close returns to entry.
    candles[51] = [51 * 60_000, 100.0, 107.0, 97.0, 100.0, 1000.0]
    calls = {"count": 0}

    def fake_analyze(_history):
        calls["count"] += 1
        return {
            "signal": "LONG" if calls["count"] == 1 else "WAIT",
            "price": 100.0,
            "atr14": 2.0,
        }

    monkeypatch.setattr(backtest, "analyze", fake_analyze)
    result = backtest.run_backtest(candles)

    assert result["open_position"]["status"] == "OPEN"
    close_drawdown = max(point["drawdown_pct"] for point in result["equity_curve"])
    assert result["max_drawdown_pct"] > close_drawdown + 0.25
    assert result["max_drawdown_pct"] > 0.5
