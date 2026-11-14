import json

import pytest

from backend import backtest
from backend.config import settings
from backend.backtest import run_backtest
from backend.indicators import analyze


def rising_candles(count=100):
    candles = []
    for i in range(count):
        close = 100 + i * 0.5
        candles.append([i * 3600000, close - 0.2, close + 1.0, close - 1.0, close, 100.0])
    return candles


def one_long_then_wait(candles):
    # The first closed candle generates a LONG; all subsequent candles wait.
    calls = {"count": 0}

    def fake_analyze(_history):
        calls["count"] += 1
        return {
            "signal": "LONG" if calls["count"] == 1 else "WAIT",
            "price": 100.0,
            "atr14": 2.0,
        }

    return fake_analyze


def exit_on_target_candles():
    candles = [[i * 3600000, 100.0, 101.0, 99.0, 100.0, 100.0] for i in range(60)]
    candles[50] = [50 * 3600000, 100.0, 102.0, 99.0, 101.0, 100.0]
    candles[51] = [51 * 3600000, 100.0, 110.0, 99.0, 108.0, 100.0]
    return candles


def test_adx_and_quality_filters_are_reported():
    metrics = analyze(rising_candles(80))
    assert metrics["adx14"] >= 20
    assert metrics["volume_ratio"] == pytest.approx(1.0)
    assert metrics["signal"] == "LONG"


def test_backtest_enters_next_bar_and_reports_cost_assumptions(monkeypatch):
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    result = run_backtest(exit_on_target_candles())
    assert result["trade_count"] > 0
    assert result["assumptions"]["entry"] == "next candle open"
    assert result["assumptions"]["same_candle_stop_and_target"] == "stop first"
    assert result["assumptions"]["funding"] == "not modeled"
    assert all("r_multiple" in trade and "costs" in trade for trade in result["trades"])
    assert result["profit_factor"] is None
    json.dumps(result, allow_nan=False)


def test_backtest_requires_enough_history():
    with pytest.raises(ValueError, match="At least 52 candles"):
        run_backtest(rising_candles(40))


def test_backtest_rejects_non_finite_starting_balance():
    with pytest.raises(ValueError, match="finite and positive"):
        run_backtest(rising_candles(), starting_balance=float("inf"))


@pytest.mark.parametrize(
    "contract_type,contract_size,precision,expected_gross_pnl",
    [
        ("linear", 0.01, 1, 80.0),
        ("inverse", 100.0, 1, 100.0),
    ],
)
def test_futures_backtest_uses_contract_value_and_precision(
    monkeypatch, contract_type, contract_size, precision, expected_gross_pnl
):
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)
    monkeypatch.setattr(settings, "slippage_bps", 0.0)
    monkeypatch.setattr(settings, "risk_per_trade_pct", 0.5)
    monkeypatch.setattr(settings, "max_position_notional_pct", 20.0)
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    result = run_backtest(
        exit_on_target_candles(),
        instrument={
            "contract_type": contract_type,
            "contract_size": contract_size,
            "amount_precision": precision,
            "precision_mode": 2,
            "min_amount": 0.1,
            "max_amount": 1000,
            "market_type": "futures",
        },
    )
    assert result["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["contract_type"] == contract_type
    assert trade["contract_size"] == pytest.approx(contract_size)
    assert trade["quantity"] > 0
    assert trade["quantity"] == round(trade["quantity"], 1)
    assert trade["notional"] <= 2000
    assert trade["gross_pnl"] == pytest.approx(expected_gross_pnl, rel=0.05)
    assert result["instrument"]["contract_type"] == contract_type


def test_futures_backtest_rejects_missing_contract_precision(monkeypatch):
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    result = run_backtest(
        exit_on_target_candles(),
        instrument={
            "contract_type": "linear",
            "contract_size": 0.001,
            "market_type": "futures",
        },
    )
    assert result["trade_count"] == 0
    assert "precision" in result["skipped_signals"][0]["reason"]


def test_backtest_profit_factor_is_json_safe_when_no_losses(monkeypatch):
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    result = run_backtest(exit_on_target_candles())
    assert result["profit_factor"] is None
    assert "Infinity" not in json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("threshold", [0.8, 1.0, 1.2])
def test_volume_ratio_thresholds_are_configurable_at_08_10_12(threshold):
    candles = rising_candles(80)
    # The previous 20 closed candles average exactly 100 volume units.
    candles[-1][5] = threshold * 100
    metrics = analyze(candles, volume_ratio_threshold=threshold)
    assert metrics["volume_ratio"] == pytest.approx(threshold)
    assert metrics["volume_ratio_threshold"] == pytest.approx(threshold)
    assert metrics["signal"] == "LONG"
    assert "VOLUME_BELOW_AVERAGE" not in metrics["filter_reasons"]


def test_volume_ratio_below_selected_threshold_is_filtered():
    candles = rising_candles(80)
    candles[-1][5] = 80.0
    metrics = analyze(candles, volume_ratio_threshold=1.0)
    assert metrics["volume_ratio"] == pytest.approx(0.8)
    assert metrics["signal"] == "WAIT"
    assert "VOLUME_BELOW_AVERAGE" in metrics["filter_reasons"]


def test_backtest_passes_experimental_volume_threshold_and_reports_it(monkeypatch):
    observed = []

    def fake_analyze(_candles, volume_ratio_threshold=None):
        observed.append(volume_ratio_threshold)
        return {"signal": "WAIT", "price": 100.0, "atr14": 2.0}

    monkeypatch.setattr(backtest, "analyze", fake_analyze)
    result = run_backtest(
        rising_candles(60),
        volume_ratio_threshold=0.8,
    )
    assert observed
    assert set(observed) == {0.8}
    assert result["assumptions"]["volume_ratio_threshold"] == 0.8


def test_backtest_rejects_invalid_volume_ratio_threshold():
    with pytest.raises(ValueError, match="volume_ratio_threshold"):
        run_backtest(rising_candles(60), volume_ratio_threshold=float("nan"))



def test_backtest_target_uses_configured_reward_risk_ratio(monkeypatch):
    monkeypatch.setattr(settings, "reward_risk_ratio", 3.0)
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    candles = [
        [i * 3600000, 100.0, 101.0, 99.0, 100.0, 100.0]
        for i in range(60)
    ]

    result = run_backtest(candles)

    assert result["open_position"]["target"] == pytest.approx(112.0)
    assert result["assumptions"]["reward_risk_ratio"] == pytest.approx(3.0)
