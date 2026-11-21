import pytest
from backend.binance_usdm_liquidation import MaintenanceBracket,calculate_isolated_liquidation_price

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
