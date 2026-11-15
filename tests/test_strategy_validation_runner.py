import pytest

from scripts import run_strategy_validation


def test_strategy_validation_defaults_to_multiple_timeframes(monkeypatch):
    monkeypatch.delenv("STRATEGY_VALIDATION_TIMEFRAMES", raising=False)
    assert run_strategy_validation._parse_timeframes() == ("1h", "4h")


def test_strategy_validation_accepts_pre_registered_timeframe_matrix(monkeypatch):
    monkeypatch.setenv("STRATEGY_VALIDATION_TIMEFRAMES", "15m,1h,4h,1h")
    assert run_strategy_validation._parse_timeframes() == ("15m", "1h", "4h")


def test_strategy_validation_rejects_unsupported_timeframes(monkeypatch):
    monkeypatch.setenv("STRATEGY_VALIDATION_TIMEFRAMES", "1h,3m")
    with pytest.raises(ValueError, match="unsupported strategy-validation timeframe"):
        run_strategy_validation._parse_timeframes()
