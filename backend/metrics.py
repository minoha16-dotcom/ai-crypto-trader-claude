"""Shared paper-trading P&L and risk calculations in quote currency."""
from .config import settings
from .contracts import contract_gross_pnl, contract_notional, contract_risk_amount


def trading_cost_rate():
    return float(settings.taker_fee_rate) + float(settings.slippage_bps) / 10000.0


def position_pnl(entry, quantity, side, mark_price, contract_size=1.0, contract_type="spot"):
    entry = float(entry)
    quantity = float(quantity)
    mark_price = float(mark_price)
    gross = contract_gross_pnl(entry, mark_price, quantity, side, contract_size, contract_type)
    rate = trading_cost_rate()
    entry_cost = contract_notional(entry, quantity, contract_size, contract_type) * rate
    exit_cost = contract_notional(mark_price, quantity, contract_size, contract_type) * rate
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
    raw_risk = contract_risk_amount(entry, stop, quantity, contract_size, contract_type)
    costs = (
        contract_notional(entry, quantity, contract_size, contract_type)
        + contract_notional(stop, quantity, contract_size, contract_type)
    ) * trading_cost_rate()
    return raw_risk + costs
