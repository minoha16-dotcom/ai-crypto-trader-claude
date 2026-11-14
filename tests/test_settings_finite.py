import math

import pytest
from pydantic import ValidationError

from backend.config import Settings


FLOAT_SETTINGS = (
    "risk_per_trade_pct",
    "max_open_risk_pct",
    "max_daily_loss_pct",
    "max_position_notional_pct",
    "taker_fee_rate",
    "slippage_bps",
    "signal_min_adx",
    "signal_min_volume_ratio",
    "signal_min_atr_pct",
    "paper_starting_balance",
    "paper_entry_max_deviation_pct",
    "position_monitor_max_price_age_seconds",
    "max_futures_leverage",
    "max_futures_margin_pct",
    "paper_futures_default_funding_rate",
    "paper_futures_funding_interval_hours",
    "paper_futures_maintenance_margin_rate",
    "paper_futures_liquidation_fee_rate",
    "reward_risk_ratio",
)


@pytest.mark.parametrize("field", FLOAT_SETTINGS)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_float_settings_reject_non_finite_values(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_default_cost_and_risk_settings_are_valid():
    config = Settings(_env_file=None)
    assert config.risk_per_trade_pct <= config.max_open_risk_pct
    assert config.taker_fee_rate >= 0
    assert config.slippage_bps >= 0


@pytest.mark.parametrize("exchange", ["kucoin", "bybit", "okx", " KUCOIN "])
def test_default_exchange_is_normalized_to_supported_platform(exchange):
    config = Settings(_env_file=None, default_exchange=exchange)
    assert config.default_exchange == exchange.strip().lower()


def test_default_exchange_rejects_legacy_only_binance():
    with pytest.raises(ValidationError, match="default_exchange"):
        Settings(_env_file=None, default_exchange="binance")


@pytest.mark.parametrize("max_open_trades", [0, 101, -1])
def test_max_open_trades_must_be_bounded(max_open_trades):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_open_trades=max_open_trades)


def test_configuration_keeps_existing_paper_limits_as_defaults():
    config = Settings(_env_file=None)
    assert config.max_open_trades == 3
    assert config.reward_risk_ratio == 2.0



def test_isolated_futures_defaults_are_conservative_and_bounded():
    config = Settings(_env_file=None)
    assert config.max_futures_leverage == 5.0
    assert config.max_futures_margin_pct == 50.0
    assert config.paper_futures_default_funding_rate == pytest.approx(0.0001)
    assert config.paper_futures_funding_interval_hours == 8.0
    assert config.paper_futures_maintenance_margin_rate == pytest.approx(0.005)
    assert config.paper_futures_liquidation_fee_rate == pytest.approx(0.005)


@pytest.mark.parametrize("hostname", ["bybit.com", "BYBIT.COM", "bytick.com"])
def test_bybit_api_hostname_accepts_only_official_global_hosts(hostname):
    config = Settings(_env_file=None, bybit_api_hostname=hostname)
    assert config.bybit_api_hostname == hostname.strip().lower()


@pytest.mark.parametrize("hostname", ["localhost", "example.com", "api.bybit.com", ""])
def test_bybit_api_hostname_rejects_unapproved_hosts(hostname):
    with pytest.raises(ValidationError, match="bybit_api_hostname"):
        Settings(_env_file=None, bybit_api_hostname=hostname)
