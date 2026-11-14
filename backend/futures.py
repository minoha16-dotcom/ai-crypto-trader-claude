"""Conservative isolated-margin Futures simulation math.

This module models quote-settled linear and inverse contracts with a fixed
maintenance-margin rate. It is a research approximation, not an exchange's
tiered liquidation engine or a guarantee of execution price.
"""
import math

from .contracts import contract_notional


def _finite(value, name):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def validate_futures_parameters(
    *,
    leverage,
    funding_rate,
    funding_interval_hours,
    maintenance_margin_rate,
    liquidation_fee_rate,
    max_leverage,
):
    leverage = _finite(leverage, "leverage")
    funding_rate = _finite(funding_rate, "funding_rate")
    interval = _finite(funding_interval_hours, "funding_interval_hours")
    maintenance = _finite(maintenance_margin_rate, "maintenance_margin_rate")
    liquidation_fee = _finite(liquidation_fee_rate, "liquidation_fee_rate")
    if leverage < 1 or leverage > float(max_leverage):
        raise ValueError(f"leverage must be between 1 and {float(max_leverage):g}x")
    if abs(funding_rate) > 0.02:
        raise ValueError("funding_rate must be between -0.02 and 0.02 per settlement")
    if not 0 < interval <= 24:
        raise ValueError("funding_interval_hours must be greater than 0 and at most 24")
    if not 0 < maintenance < 0.2:
        raise ValueError("maintenance_margin_rate must be greater than 0 and below 0.20")
    if not 0 <= liquidation_fee < 0.05:
        raise ValueError("liquidation_fee_rate must be between 0 and 0.05")
    if maintenance + liquidation_fee + 2 * 0.01 >= 1:
        raise ValueError("Combined maintenance and liquidation costs are not viable")
    return {
        "leverage": leverage,
        "funding_rate": funding_rate,
        "funding_interval_hours": interval,
        "maintenance_margin_rate": maintenance,
        "liquidation_fee_rate": liquidation_fee,
    }


def initial_margin(notional, leverage):
    notional = _finite(notional, "notional")
    leverage = _finite(leverage, "leverage")
    if notional <= 0 or leverage < 1:
        raise ValueError("notional must be positive and leverage must be at least 1")
    return notional / leverage


def funding_payment(notional, funding_rate, side):
    """Return signed funding cost: positive is paid, negative is received."""
    notional = _finite(notional, "notional")
    rate = _finite(funding_rate, "funding_rate")
    if notional < 0 or abs(rate) > 0.02:
        raise ValueError("Invalid notional or funding rate")
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    direction = 1.0 if side == "LONG" else -1.0
    return notional * rate * direction


def due_funding(mark_price, quantity, contract_size, contract_type, side,
                funding_rate, next_funding_at, now, interval_hours):
    """Return (signed payment, due event count, next UTC epoch timestamp)."""
    next_at = _finite(next_funding_at, "next_funding_at")
    current = _finite(now, "now")
    interval = _finite(interval_hours, "funding_interval_hours") * 3600.0
    if interval <= 0:
        raise ValueError("funding interval must be positive")
    if current < next_at:
        return 0.0, 0, next_at
    events = int(math.floor((current - next_at) / interval)) + 1
    notional = contract_notional(
        mark_price, quantity, contract_size, contract_type
    )
    payment = funding_payment(notional, funding_rate, side) * events
    return payment, events, next_at + events * interval


def liquidation_price(
    entry,
    quantity,
    contract_size,
    contract_type,
    side,
    leverage,
    maintenance_margin_rate,
    fee_rate,
    liquidation_fee_rate,
    funding_paid=0.0,
):
    """Estimate isolated liquidation price after entry/exit costs and funding."""
    entry = _finite(entry, "entry")
    quantity = _finite(quantity, "quantity")
    size = _finite(contract_size, "contract_size")
    leverage = _finite(leverage, "leverage")
    maintenance = _finite(maintenance_margin_rate, "maintenance_margin_rate")
    fee = _finite(fee_rate, "fee_rate")
    liquidation_fee = _finite(liquidation_fee_rate, "liquidation_fee_rate")
    funding = _finite(funding_paid, "funding_paid")
    if entry <= 0 or quantity <= 0 or size <= 0 or leverage < 1:
        raise ValueError("Invalid entry, quantity, contract size, or leverage")
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    if contract_type not in ("linear", "inverse"):
        raise ValueError("Isolated Futures margin supports linear or inverse contracts only")
    if not 0 < maintenance < 0.2 or not 0 <= fee < 1 or not 0 <= liquidation_fee < 0.05:
        raise ValueError("Invalid maintenance or fee rate")

    entry_notional = contract_notional(entry, quantity, size, contract_type)
    margin = initial_margin(entry_notional, leverage)
    if contract_type == "linear":
        base_units = quantity * size
        if side == "LONG":
            numerator = entry_notional - margin + entry_notional * fee + funding
            denominator = base_units * (1 - fee - maintenance - liquidation_fee)
            # At 1x with zero entry/exit fees and no funding, the isolated
            # long has no positive-price liquidation threshold. Preserve that
            # as None instead of claiming a synthetic positive liquidation.
            if numerator <= 0:
                return None
        else:
            numerator = margin + entry_notional - entry_notional * fee - funding
            denominator = base_units * (1 + fee + maintenance + liquidation_fee)
        if denominator <= 0:
            raise ValueError("Liquidation denominator is not positive")
        price = numerator / denominator
    else:
        # Inverse P&L is converted to quote currency at the mark price. This
        # matches contract_gross_pnl's quote-equivalent convention and is not
        # a base-coin collateral or venue-specific tier model.
        funding_fraction = funding / entry_notional
        total_cost_fraction = fee + fee + maintenance + liquidation_fee
        if side == "LONG":
            factor = 1 - 1 / leverage + total_cost_fraction + funding_fraction
        else:
            factor = 1 + 1 / leverage - total_cost_fraction - funding_fraction
        if factor <= 0:
            return None
        price = entry * factor

    if not math.isfinite(price) or price <= 0:
        raise ValueError("Calculated liquidation price is not positive and finite")
    return price


def liquidation_triggered(side, mark_price, liquidation_price_value):
    mark = _finite(mark_price, "mark_price")
    liq = _finite(liquidation_price_value, "liquidation_price")
    if side == "LONG":
        return mark <= liq
    if side == "SHORT":
        return mark >= liq
    raise ValueError("side must be LONG or SHORT")


def margin_snapshot(
    entry, mark, quantity, contract_size, contract_type, side,
    leverage, maintenance_margin_rate, initial_margin_value,
    unrealized_pnl, liquidation_price_value, funding_paid,
):
    mark_notional = contract_notional(
        mark, quantity, contract_size, contract_type
    )
    maintenance = mark_notional * float(maintenance_margin_rate)
    margin_equity = float(initial_margin_value) + float(unrealized_pnl)
    liq_value = (
        float(liquidation_price_value)
        if liquidation_price_value is not None else None
    )
    return {
        "leverage": float(leverage),
        "initial_margin": float(initial_margin_value),
        "maintenance_margin": maintenance,
        "margin_equity": margin_equity,
        "margin_ratio_pct": (
            margin_equity / maintenance * 100.0 if maintenance > 0 else None
        ),
        "liquidation_price": liq_value,
        "liquidation_distance_pct": (
            abs(float(mark) - liq_value) / float(mark) * 100.0
            if liq_value is not None else None
        ),
        "funding_paid": float(funding_paid),
        "is_liquidated": (
            liquidation_triggered(side, mark, liq_value)
            if liq_value is not None else False
        ),
    }
