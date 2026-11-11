"""Exchange contract normalization and quote-currency math for paper trading."""
from decimal import Decimal, ROUND_DOWN
import math


def _positive(value, name):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"Invalid {name}: expected a finite positive number")
    return number


def normalize_amount(amount, precision, precision_mode, min_amount=None, max_amount=None):
    """Round contracts down using CCXT precision semantics, then enforce limits."""
    amount = _positive(amount, "amount")
    if precision is None:
        raise ValueError("Exchange did not provide amount precision")
    precision = float(precision)
    if not math.isfinite(precision) or precision < 0 or (precision_mode != 2 and precision == 0):
        raise ValueError("Invalid exchange amount precision")
    if precision_mode == 4:  # CCXT TICK_SIZE
        step = Decimal(str(precision))
    elif precision_mode == 2:  # CCXT DECIMAL_PLACES
        step = Decimal(10) ** -int(precision)
    elif precision_mode == 3:  # CCXT SIGNIFICANT_DIGITS
        exponent = math.floor(math.log10(amount)) - int(precision) + 1
        step = Decimal(10) ** exponent
    else:
        raise ValueError(f"Unsupported CCXT precision mode: {precision_mode}")
    rounded = (Decimal(str(amount)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    result = float(rounded)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Calculated order amount is below exchange precision")
    if min_amount is not None and result < float(min_amount):
        raise ValueError(f"Amount {result} is below exchange minimum {min_amount}")
    if max_amount is not None and result > float(max_amount):
        raise ValueError(f"Amount {result} exceeds exchange maximum {max_amount}")
    return result


def validate_instrument(market_type, market):
    """Extract validated, normalized CCXT market metadata."""
    amount_precision = (market.get("precision") or {}).get("amount")
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}
    if market_type == "spot":
        contract_type = "spot"
        contract_size = 1.0
    else:
        if not market.get("contract") or not (market.get("swap") or market.get("future")):
            raise ValueError("Selected market is not a supported Futures contract")
        linear = bool(market.get("linear"))
        inverse = bool(market.get("inverse"))
        if linear == inverse:
            raise ValueError("Futures contract must be explicitly linear or inverse")
        contract_type = "linear" if linear else "inverse"
        contract_size = _positive(market.get("contractSize"), "contractSize")
    if market_type == "futures" and amount_precision is None:
        raise ValueError("Exchange did not publish amount precision for this Futures market")
    return {
        "contract_size": contract_size,
        "contract_type": contract_type,
        "precision_mode": market.get("_precisionMode"),
        "amount_precision": amount_precision,
        "min_amount": amount_limits.get("min"),
        "max_amount": amount_limits.get("max"),
        "min_cost": cost_limits.get("min"),
        "max_cost": cost_limits.get("max"),
        "base": market.get("base"),
        "quote": market.get("quote"),
        "settle": market.get("settle"),
    }


def contract_notional(entry, quantity, contract_size=1.0, contract_type="spot"):
    entry = _positive(entry, "entry")
    quantity = _positive(quantity, "quantity")
    size = _positive(contract_size, "contract_size")
    return quantity * size if contract_type == "inverse" else quantity * size * entry


def contract_gross_pnl(entry, mark, quantity, side, contract_size=1.0, contract_type="spot"):
    entry = _positive(entry, "entry")
    mark = _positive(mark, "mark")
    quantity = _positive(quantity, "quantity")
    size = _positive(contract_size, "contract_size")
    direction = 1.0 if side == "LONG" else -1.0
    if contract_type == "inverse":
        # Inverse contracts settle in base currency; convert the settlement P&L
        # back to quote currency at the mark price for the paper account.
        return direction * quantity * size * (mark / entry - 1.0)
    return direction * quantity * size * (mark - entry)


def contract_risk_amount(entry, stop, quantity, contract_size=1.0, contract_type="spot"):
    entry = _positive(entry, "entry")
    stop = _positive(stop, "stop")
    quantity = _positive(quantity, "quantity")
    size = _positive(contract_size, "contract_size")
    if contract_type == "inverse":
        return quantity * size * abs(stop / entry - 1.0)
    return quantity * size * abs(entry - stop)
