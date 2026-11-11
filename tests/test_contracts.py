import pytest

from backend.contracts import (
    contract_gross_pnl, contract_notional, contract_risk_amount,
    normalize_amount, validate_instrument,
)


def test_linear_contract_value_scales_with_contract_size():
    assert contract_notional(100, 3, 0.01, "linear") == pytest.approx(3.0)
    assert contract_gross_pnl(100, 110, 3, "LONG", 0.01, "linear") == pytest.approx(0.3)
    assert contract_risk_amount(100, 95, 3, 0.01, "linear") == pytest.approx(0.15)


def test_inverse_contract_pnl_uses_quote_face_value():
    # 3 inverse contracts with a 100 USD face value each.
    assert contract_notional(100, 3, 100, "inverse") == pytest.approx(300)
    assert contract_gross_pnl(100, 110, 3, "LONG", 100, "inverse") == pytest.approx(30)
    assert contract_gross_pnl(100, 90, 3, "SHORT", 100, "inverse") == pytest.approx(30)


@pytest.mark.parametrize(
    "amount,precision,mode,expected",
    [
        (1.239, 3, 2, 1.239),       # decimal places
        (1.2399, 0.01, 4, 1.23),    # tick size
        (1234.9, 3, 3, 1230.0),     # significant digits
    ],
)
def test_amount_is_rounded_down_to_exchange_precision(amount, precision, mode, expected):
    assert normalize_amount(amount, precision, mode) == pytest.approx(expected)


def test_amount_below_exchange_minimum_is_rejected():
    with pytest.raises(ValueError, match="below exchange minimum"):
        normalize_amount(0.0004, 3, 2, min_amount=0.001)


def test_futures_market_requires_contract_metadata():
    with pytest.raises(ValueError, match="supported Futures contract"):
        validate_instrument("futures", {
            "contract": False, "swap": False, "precision": {"amount": 3},
        })


def test_futures_market_extracts_linear_and_inverse_type():
    linear = validate_instrument("futures", {
        "contract": True, "swap": True, "linear": True, "inverse": False,
        "contractSize": 0.001, "precision": {"amount": 3},
        "limits": {"amount": {"min": 0.001, "max": 1000}},
    })
    inverse = validate_instrument("futures", {
        "contract": True, "future": True, "linear": False, "inverse": True,
        "contractSize": 100, "precision": {"amount": 1},
    })
    assert linear["contract_type"] == "linear"
    assert linear["contract_size"] == pytest.approx(0.001)
    assert inverse["contract_type"] == "inverse"
    assert inverse["contract_size"] == pytest.approx(100)
