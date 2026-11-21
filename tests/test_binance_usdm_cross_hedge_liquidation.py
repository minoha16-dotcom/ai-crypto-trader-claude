import pytest

from backend.binance_usdm_cross_hedge_liquidation import (
    MaintenanceBracket, calculate_binance_usdm_liquidation_price,
)

TIERS = [MaintenanceBracket(0, 50_000, 0.005, 0)]


def test_cross_one_way_long():
    result = calculate_binance_usdm_liquidation_price(
        margin_mode="CROSSED", position_mode="ONE_WAY",
        wallet_balance=1000, long_qty=0.1, long_entry_price=50000,
        long_brackets=TIERS, other_maintenance_margin=50,
        other_unrealized_pnl=25,
    )
    assert result["status"] == "PASS"
    assert result["liquidation_price"] > 0


def test_cross_one_way_short():
    result = calculate_binance_usdm_liquidation_price(
        margin_mode="CROSSED", position_mode="ONE_WAY",
        wallet_balance=1000, short_qty=0.1, short_entry_price=50000,
        short_brackets=TIERS, other_maintenance_margin=50,
    )
    assert result["status"] == "PASS"
    assert result["liquidation_price"] > 0


def test_hedge_mode_both_legs():
    result = calculate_binance_usdm_liquidation_price(
        margin_mode="CROSSED", position_mode="HEDGE",
        wallet_balance=5000, long_qty=0.1, long_entry_price=50000,
        short_qty=0.08, short_entry_price=51000,
        long_brackets=TIERS, short_brackets=TIERS,
        other_maintenance_margin=100, other_unrealized_pnl=40,
    )
    assert result["status"] == "PASS"
    assert result["long_notional_at_liquidation"] > 0
    assert result["short_notional_at_liquidation"] > 0


def test_isolated_hedge_legs():
    result = calculate_binance_usdm_liquidation_price(
        margin_mode="ISOLATED", position_mode="HEDGE",
        wallet_balance=3000, long_qty=0.1, long_entry_price=50000,
        short_qty=0.05, short_entry_price=51000,
        long_brackets=TIERS, short_brackets=TIERS,
    )
    assert result["status"] == "PASS"


def test_rejects_one_way_with_two_legs():
    with pytest.raises(ValueError, match="ONE_WAY"):
        calculate_binance_usdm_liquidation_price(
            margin_mode="CROSSED", position_mode="ONE_WAY",
            wallet_balance=1000, long_qty=0.1, long_entry_price=50000,
            short_qty=0.1, short_entry_price=50000,
            long_brackets=TIERS, short_brackets=TIERS,
        )


def test_isolated_rejects_cross_account_offsets():
    with pytest.raises(ValueError, match="isolated"):
        calculate_binance_usdm_liquidation_price(
            margin_mode="ISOLATED", position_mode="ONE_WAY",
            wallet_balance=1000, long_qty=0.1, long_entry_price=50000,
            long_brackets=TIERS, other_maintenance_margin=1,
        )


def test_rejects_missing_brackets():
    with pytest.raises(ValueError, match="bracket"):
        calculate_binance_usdm_liquidation_price(
            margin_mode="CROSSED", position_mode="ONE_WAY",
            wallet_balance=1000, long_qty=0.1, long_entry_price=50000,
        )
