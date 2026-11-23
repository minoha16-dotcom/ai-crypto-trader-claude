import pytest
from backend.binance_usdm_liquidation import (MaintenanceBracket, calculate_isolated_liquidation_price, derive_brackets_from_published_tiers)

TIERS=[MaintenanceBracket(0,50_000,0.004,0),MaintenanceBracket(50_000,250_000,0.005,50)]
def test_isolated_long_formula_and_bracket():
 r=calculate_isolated_liquidation_price(side="LONG",entry_price=100,quantity=10,isolated_wallet_balance=100,brackets=TIERS)
 assert r["status"]=="PASS"
 assert r["liquidation_price"]==pytest.approx((100-1000)/(10*(.004-1))*-1) if False else r["liquidation_price"]==pytest.approx(90.3614457831)
def test_isolated_short_formula():
 r=calculate_isolated_liquidation_price(side="SHORT",entry_price=100,quantity=1,isolated_wallet_balance=20,brackets=TIERS)
 assert r["liquidation_price"]==pytest.approx(120/1.004)
def test_rejects_cross_hedge_and_invalid_brackets():
 with pytest.raises(ValueError,match="unsupported"):
  calculate_isolated_liquidation_price(side="LONG",entry_price=100,quantity=1,isolated_wallet_balance=10,brackets=TIERS,margin_mode="CROSS")
 with pytest.raises(ValueError,match="contiguous"):
  calculate_isolated_liquidation_price(side="LONG",entry_price=100,quantity=1,isolated_wallet_balance=10,brackets=[MaintenanceBracket(0,10,.01,0),MaintenanceBracket(11,20,.02,0)])



def test_derive_cumulative_maintenance_amount_from_published_tiers():
    brackets = derive_brackets_from_published_tiers([
        {"notional_floor": 0, "notional_cap": 50_000, "maint_margin_ratio": 0.004},
        {"notional_floor": 50_000, "notional_cap": 250_000, "maint_margin_ratio": 0.005},
        {"notional_floor": 250_000, "notional_cap": 500_000, "maint_margin_ratio": 0.01},
    ])
    assert [row.cum for row in brackets] == pytest.approx([0, 50, 1300])


def test_derive_cumulative_maintenance_amount_rejects_tier_gaps():
    with pytest.raises(ValueError, match="contiguous"):
        derive_brackets_from_published_tiers([
            {"notional_floor": 0, "notional_cap": 50_000, "maint_margin_ratio": 0.004},
            {"notional_floor": 50_001, "notional_cap": 250_000, "maint_margin_ratio": 0.005},
        ])
