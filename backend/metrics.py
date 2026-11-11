"""Shared paper-trading P&L and risk calculations.

All position marks are estimates in quote currency and include configured
entry/exit fees plus slippage. No exchange account balance is implied.
"""
from .config import settings


def trading_cost_rate():
    return float(settings.taker_fee_rate) + float(settings.slippage_bps) / 10000.0


def position_pnl(entry, quantity, side, mark_price):
    entry = float(entry)
    quantity = float(quantity)
    mark_price = float(mark_price)
    direction = 1.0 if side == "LONG" else -1.0
    gross = (mark_price - entry) * quantity * direction
    rate = trading_cost_rate()
    entry_cost = entry * quantity * rate
    estimated_exit_cost = mark_price * quantity * rate
    costs = entry_cost + estimated_exit_cost
    net = gross - costs
    notional = entry * quantity
    return {
        "gross_pnl": gross,
        "estimated_costs": costs,
        "unrealized_pnl": net,
        "unrealized_pnl_pct": net / notional * 100.0 if notional else 0.0,
    }


def position_risk(entry, stop, quantity):
    """Worst-case entry-to-stop loss estimate, including both-side costs."""
    entry = float(entry)
    stop = float(stop)
    quantity = float(quantity)
    return (abs(entry - stop) + (entry + stop) * trading_cost_rate()) * quantity
