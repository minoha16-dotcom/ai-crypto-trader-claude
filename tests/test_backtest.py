import json

import pytest

from backend import backtest
from backend.config import settings
from backend.backtest import run_backtest
from backend.indicators import analyze


def rising_candles(count=260):
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
    metrics = analyze(rising_candles(260))
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
    candles = rising_candles(260)
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


def test_backtest_can_start_scoring_after_warmup(monkeypatch):
    observed_lengths = []

    def fake_analyze(history, volume_ratio_threshold=None):
        observed_lengths.append(len(history))
        return {"signal": "WAIT", "price": 100.0, "atr14": 2.0}

    monkeypatch.setattr(backtest, "analyze", fake_analyze)
    result = run_backtest(rising_candles(70), start_index=55)

    assert observed_lengths
    assert observed_lengths[0] == 56
    assert result["assumptions"]["evaluation_start_index"] == 55
    assert result["assumptions"]["warmup_candles_excluded_from_scoring"] == 55


@pytest.mark.parametrize("start_index", [48, 69])
def test_backtest_rejects_invalid_evaluation_start_index(start_index):
    with pytest.raises(ValueError, match="start_index"):
        run_backtest(rising_candles(70), start_index=start_index)


def test_backtest_enforces_maximum_holding_horizon(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)
    monkeypatch.setattr(settings, "slippage_bps", 0.0)
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    candles = [
        [i * 3600000, 100.0, 101.0, 99.0, 100.0, 100.0]
        for i in range(60)
    ]

    result = run_backtest(candles, max_holding_bars=2)

    assert result["trade_count"] == 1
    assert result["trades"][0]["exit_reason"] == "TIME_EXIT"
    assert result["trades"][0]["exit_index"] == 51
    assert result["open_positions"] == []
    assert result["assumptions"]["max_holding_bars"] == 2


def test_backtest_can_force_close_remaining_position_at_period_end(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)
    monkeypatch.setattr(settings, "slippage_bps", 0.0)
    monkeypatch.setattr(backtest, "analyze", one_long_then_wait(rising_candles()))
    candles = [
        [i * 3600000, 100.0, 101.0, 99.0, 100.0, 100.0]
        for i in range(60)
    ]

    result = run_backtest(candles, close_open_at_end=True)

    assert result["trade_count"] == 1
    assert result["trades"][0]["exit_reason"] == "PERIOD_END"
    assert result["trades"][0]["exit_index"] == 59
    assert result["open_positions"] == []
    assert result["unrealized_pnl"] == pytest.approx(0.0)
    assert result["assumptions"]["open_at_end"] == "forced close at final candle close"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_backtest_rejects_invalid_maximum_holding_horizon(value):
    with pytest.raises(ValueError, match="max_holding_bars"):
        run_backtest(rising_candles(60), max_holding_bars=value)


def test_backtest_records_each_wait_rejection_with_context(monkeypatch):
    def rejected(_history):
        return {
            "signal": "WAIT",
            "direction": "neutral",
            "filter_reasons": ["REGIME_RANGING", "VOLUME_BELOW_AVERAGE"],
            "price": 100.0,
            "atr14": 2.0,
            "adx14": 12.0,
            "plus_di14": 18.0,
            "minus_di14": 20.0,
            "ema20": 99.0,
            "ema50": 100.0,
            "ema200": 101.0,
            "rsi14": 45.0,
            "atr_pct": 2.0,
            "volume_ratio": 0.7,
            "volume_ratio_threshold": 1.0,
            "net_reward_risk": 1.2,
        }

    monkeypatch.setattr(backtest, "analyze", rejected)
    result = run_backtest(rising_candles(60), start_index=55)

    assert len(result["skipped_signals"]) == 2 * 4
    assert {row["reason"] for row in result["skipped_signals"]} == {
        "REGIME_RANGING", "VOLUME_BELOW_AVERAGE"
    }
    first = result["skipped_signals"][0]
    assert first["stage"] == "signal_eligibility"
    assert first["timestamp"] == rising_candles(60)[55][0]
    assert first["side"] == "NEUTRAL"
    assert first["indicators"]["adx14"] == 12.0



def test_direction_filter_excludes_opposite_signals_and_is_reported(monkeypatch):
    def short_signal(_history):
        return {"signal": "SHORT", "direction": "bearish", "price": 100.0, "atr14": 2.0}

    monkeypatch.setattr(backtest, "analyze", short_signal)
    result = run_backtest(
        rising_candles(60), start_index=55, direction_filter="LONG"
    )

    assert result["trade_count"] == 0
    assert result["assumptions"]["direction_filter"] == "LONG"
    assert all(
        row["reason"] == "EXPERIMENT_DIRECTION_FILTER_LONG"
        and row["stage"] == "experiment_filter"
        and row["side"] == "SHORT"
        for row in result["skipped_signals"]
    )


@pytest.mark.parametrize("direction_filter", ["BOTH", "NEUTRAL", ""])
def test_direction_filter_rejects_invalid_values(direction_filter):
    with pytest.raises(ValueError, match="direction_filter"):
        run_backtest(rising_candles(60), direction_filter=direction_filter)



def test_long_ema_gates_can_be_tested_independently(monkeypatch):
    from backend import indicators

    monkeypatch.setattr(
        indicators, "directional_indicators",
        lambda _rows: {"adx14": 30.0, "plus_di14": 30.0, "minus_di14": 10.0},
    )
    monkeypatch.setattr(indicators, "ema", lambda _values, period: {
        20: 120.0, 50: 110.0, 200: 300.0
    }[period])
    monkeypatch.setattr(indicators, "atr", lambda _rows: 2.0)
    monkeypatch.setattr(indicators, "rsi", lambda _values: 60.0)

    rows = rising_candles(260)
    baseline = indicators.analyze(rows)
    relaxed = indicators.analyze(rows, signal_overrides={"long_require_ema200": False})

    assert baseline["signal"] == "WAIT"
    assert relaxed["signal"] == "LONG"
    assert relaxed["long_gate_diagnostics"]["price_above_ema200"] is False
    assert relaxed["signal_overrides"] == {"long_require_ema200": False}


def test_long_ema20_50_gate_can_be_tested_independently(monkeypatch):
    from backend import indicators

    monkeypatch.setattr(
        indicators, "directional_indicators",
        lambda _rows: {"adx14": 30.0, "plus_di14": 30.0, "minus_di14": 10.0},
    )
    monkeypatch.setattr(indicators, "ema", lambda _values, period: {
        20: 100.0, 50: 110.0, 200: 200.0
    }[period])
    monkeypatch.setattr(indicators, "atr", lambda _rows: 2.0)
    monkeypatch.setattr(indicators, "rsi", lambda _values: 60.0)

    rows = rising_candles(260)
    baseline = indicators.analyze(rows)
    relaxed = indicators.analyze(rows, signal_overrides={"long_require_ema20_50": False})

    assert baseline["signal"] == "WAIT"
    assert relaxed["signal"] == "LONG"
    assert relaxed["long_gate_diagnostics"]["ema20_above_ema50"] is False


def test_adx_threshold_experiment_changes_only_adx_gate(monkeypatch):
    from backend import indicators

    monkeypatch.setattr(
        indicators, "directional_indicators",
        lambda _rows: {"adx14": 18.0, "plus_di14": 30.0, "minus_di14": 10.0},
    )
    monkeypatch.setattr(indicators, "ema", lambda _values, period: {
        20: 120.0, 50: 110.0, 200: 200.0
    }[period])
    monkeypatch.setattr(indicators, "atr", lambda _rows: 2.0)
    monkeypatch.setattr(indicators, "rsi", lambda _values: 60.0)

    rows = rising_candles(260)
    baseline = indicators.analyze(rows)
    relaxed = indicators.analyze(rows, signal_overrides={"adx_min": 15.0})

    assert baseline["signal"] == "WAIT"
    assert "REGIME_RANGING" in baseline["filter_reasons"]
    assert relaxed["signal"] == "LONG"
    assert relaxed["long_gate_diagnostics"]["adx_threshold"] == 15.0


def test_signal_overrides_reject_unknown_or_invalid_values():
    rows = rising_candles(260)
    with pytest.raises(ValueError, match="Unsupported signal override"):
        analyze(rows, signal_overrides={"rsi_min": 55})
    with pytest.raises(ValueError, match="adx_min override"):
        analyze(rows, signal_overrides={"adx_min": 101})
    with pytest.raises(ValueError, match="EMA signal overrides"):
        analyze(rows, signal_overrides={"long_require_ema200": 0})
