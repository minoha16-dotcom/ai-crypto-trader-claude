import pytest

from backend.futures import (
    due_funding,
    funding_payment,
    initial_margin,
    liquidation_price,
    liquidation_triggered,
    margin_snapshot,
    validate_futures_parameters,
)


def test_funding_payment_sign_matches_long_and_short_convention():
    assert funding_payment(10_000, 0.001, "LONG") == pytest.approx(10.0)
    assert funding_payment(10_000, 0.001, "SHORT") == pytest.approx(-10.0)
    assert funding_payment(10_000, -0.001, "LONG") == pytest.approx(-10.0)
    assert funding_payment(10_000, -0.001, "SHORT") == pytest.approx(10.0)


def test_due_funding_accrues_all_elapsed_intervals_once():
    # Three 8-hour funding settlements were due by 25 hours after open.
    payment, events, next_at = due_funding(
        mark_price=100,
        quantity=2,
        contract_size=1,
        contract_type="linear",
        side="LONG",
        funding_rate=0.001,
        next_funding_at=8 * 3600,
        now=25 * 3600,
        interval_hours=8,
    )
    assert events == 3
    assert payment == pytest.approx(0.6)
    assert next_at == pytest.approx(32 * 3600)

    repeated, repeated_events, unchanged_next = due_funding(
        100, 2, 1, "linear", "LONG", 0.001, next_at, 25 * 3600, 8
    )
    assert repeated == 0
    assert repeated_events == 0
    assert unchanged_next == next_at


def test_initial_margin_is_notional_divided_by_leverage():
    assert initial_margin(1_000, 5) == pytest.approx(200)


@pytest.mark.parametrize(
    "side, expected",
    [
        ("LONG", "below"),
        ("SHORT", "above"),
    ],
)
def test_linear_liquidation_price_is_on_adverse_side_of_entry(side, expected):
    price = liquidation_price(
        entry=100,
        quantity=2,
        contract_size=1,
        contract_type="linear",
        side=side,
        leverage=5,
        maintenance_margin_rate=0.005,
        fee_rate=0.0008,
        liquidation_fee_rate=0.005,
    )
    assert price < 100 if expected == "below" else price > 100


def test_liquidation_trigger_is_side_aware():
    assert liquidation_triggered("LONG", 80, 81)
    assert not liquidation_triggered("LONG", 82, 81)
    assert liquidation_triggered("SHORT", 120, 119)
    assert not liquidation_triggered("SHORT", 118, 119)


def test_inverse_liquidation_uses_quote_equivalent_margin_model():
    long_price = liquidation_price(
        100, 2, 100, "inverse", "LONG", 5, 0.005, 0.0008, 0.005
    )
    short_price = liquidation_price(
        100, 2, 100, "inverse", "SHORT", 5, 0.005, 0.0008, 0.005
    )
    assert 0 < long_price < 100 < short_price


def test_margin_snapshot_exposes_margin_ratio_and_funding():
    result = margin_snapshot(
        entry=100, mark=105, quantity=2, contract_size=1,
        contract_type="linear", side="LONG", leverage=5,
        maintenance_margin_rate=0.005, initial_margin_value=40,
        unrealized_pnl=9, liquidation_price_value=81, funding_paid=1,
    )
    assert result["initial_margin"] == pytest.approx(40)
    assert result["maintenance_margin"] == pytest.approx(1.05)
    assert result["margin_equity"] == pytest.approx(49)
    assert result["margin_ratio_pct"] == pytest.approx(49 / 1.05 * 100)
    assert result["funding_paid"] == pytest.approx(1)
    assert result["is_liquidated"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"leverage": 0.5},
        {"leverage": 6},
        {"funding_rate": 0.03},
        {"funding_interval_hours": 0},
        {"maintenance_margin_rate": 0.2},
        {"liquidation_fee_rate": 0.05},
    ],
)
def test_futures_parameters_reject_unsafe_values(overrides):
    values = {
        "leverage": 2,
        "funding_rate": 0.0001,
        "funding_interval_hours": 8,
        "maintenance_margin_rate": 0.005,
        "liquidation_fee_rate": 0.005,
        "max_leverage": 5,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        validate_futures_parameters(**values)
