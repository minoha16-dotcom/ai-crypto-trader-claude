import pytest

from backend.indicators import ema, rsi, atr, analyze
from backend.risk import RiskManager


def rising_candles(count=120):
    candles = []
    for i in range(count):
        close = 100 + i * 0.5
        candles.append([i, close - 0.2, close + 1, close - 1, close, 1000])
    return candles


def test_ema_constant_series():
    assert ema([10.0] * 30, 20) == 10.0


def test_ema_rejects_insufficient_values():
    with pytest.raises(ValueError):
        ema([1.0, 2.0], 20)


def test_rsi_rising_series():
    assert rsi([float(x) for x in range(20, 40)], 14) == 100.0


def test_rsi_flat_series_is_neutral():
    assert rsi([5.0] * 30, 14) == 50.0


def test_rsi_falling_series_is_zero():
    assert rsi([float(40 - x) for x in range(30)], 14) == 0.0


def test_rsi_rejects_insufficient_values():
    with pytest.raises(ValueError):
        rsi([1.0] * 14, 14)


def test_atr_positive_for_rising_candles():
    assert atr(rising_candles()) > 0


def test_atr_rejects_invalid_period():
    with pytest.raises(ValueError):
        atr(rising_candles(), 0)


def test_analyze_returns_signal_schema():
    result = analyze(rising_candles())
    assert result["signal"] in ("LONG", "SHORT", "WAIT")
    assert result["atr14"] > 0
    assert result["ema20"] > 0
    assert result["ema50"] > 0
    assert 0 <= result["rsi14"] <= 100


def test_analyze_rejects_insufficient_candles():
    with pytest.raises(ValueError):
        analyze(rising_candles(30))


def test_risk_caps_notional_and_quantity():
    decision = RiskManager().size(10000, 100, 95)
    assert decision.approved
    assert decision.risk_amount <= 50
    assert decision.notional <= 2000


def test_risk_rejects_excess_open_risk():
    decision = RiskManager().size(10000, 100, 95, open_risk=200)
    assert not decision.approved


def test_risk_rejects_daily_loss_limit():
    decision = RiskManager().size(10000, 100, 95, daily_pnl=-300, daily_start_equity=10000)
    assert not decision.approved


@pytest.mark.parametrize("equity,entry,stop", [
    (0, 100, 95), (1000, 0, 95), (1000, 100, 0),
    (1000, 100, 100), (-1, 100, 95),
])
def test_risk_rejects_invalid_inputs(equity, entry, stop):
    decision = RiskManager().size(equity, entry, stop)
    assert not decision.approved
    assert decision.quantity == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_risk_rejects_non_finite_inputs(value):
    decision = RiskManager().size(value, 100, 95)
    assert not decision.approved
