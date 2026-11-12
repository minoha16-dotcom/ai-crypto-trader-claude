"""Shared paper-trading P&L and risk calculations in quote currency."""
import math

from .config import settings
from .contracts import contract_gross_pnl, contract_notional, contract_risk_amount


def validate_trading_configuration():
    """Validate and normalize the cost and risk assumptions used by every engine."""
    fee_rate = float(settings.taker_fee_rate)
    slippage_bps = float(settings.slippage_bps)
    risk_pct = float(settings.risk_per_trade_pct)
    max_open_risk_pct = float(settings.max_open_risk_pct)
    max_daily_loss_pct = float(settings.max_daily_loss_pct)
    max_notional_pct = float(settings.max_position_notional_pct)
    values = (
        fee_rate, slippage_bps, risk_pct, max_open_risk_pct,
        max_daily_loss_pct, max_notional_pct,
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Trading configuration values must be finite")
    if not 0 <= fee_rate < 1:
        raise ValueError("taker_fee_rate must be between 0 inclusive and 1 exclusive")
    if not 0 <= slippage_bps < 10000:
        raise ValueError("slippage_bps must be between 0 inclusive and 10000 exclusive")
    if not 0 < risk_pct <= max_open_risk_pct <= 100:
        raise ValueError(
            "Require 0 < risk_per_trade_pct <= max_open_risk_pct <= 100"
        )
    if not 0 < max_daily_loss_pct <= 100:
        raise ValueError("max_daily_loss_pct must be greater than 0 and at most 100")
    if not 0 < max_notional_pct <= 100:
        raise ValueError(
            "max_position_notional_pct must be greater than 0 and at most 100"
        )
    return {
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "slippage": slippage_bps / 10000.0,
        "risk_pct": risk_pct,
        "max_open_risk_pct": max_open_risk_pct,
        "max_daily_loss_pct": max_daily_loss_pct,
        "max_notional_pct": max_notional_pct,
    }


def trading_cost_rate():
    config = validate_trading_configuration()
    return config["fee_rate"] + config["slippage"]


def position_pnl(entry, quantity, side, mark_price, contract_size=1.0, contract_type="spot"):
    entry = float(entry)
    quantity = float(quantity)
    mark_price = float(mark_price)
    gross = contract_gross_pnl(
        entry, mark_price, quantity, side, contract_size, contract_type
    )
    rate = trading_cost_rate()
    entry_cost = contract_notional(
        entry, quantity, contract_size, contract_type
    ) * rate
    exit_cost = contract_notional(
        mark_price, quantity, contract_size, contract_type
    ) * rate
    costs = entry_cost + exit_cost
    net = gross - costs
    notional = contract_notional(entry, quantity, contract_size, contract_type)
    return {
        "gross_pnl": gross,
        "estimated_costs": costs,
        "unrealized_pnl": net,
        "unrealized_pnl_pct": net / notional * 100.0 if notional else 0.0,
    }


def position_risk(entry, stop, quantity, contract_size=1.0, contract_type="spot"):
    """Worst-case entry-to-stop loss in quote currency, including both-side costs."""
    entry = float(entry)
    stop = float(stop)
    quantity = float(quantity)
    raw_risk = contract_risk_amount(
        entry, stop, quantity, contract_size, contract_type
    )
    costs = (
        contract_notional(entry, quantity, contract_size, contract_type)
        + contract_notional(stop, quantity, contract_size, contract_type)
    ) * trading_cost_rate()
    return raw_risk + costs
