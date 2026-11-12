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
