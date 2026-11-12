"""Exchange contract normalization and quote-currency math for paper trading."""
from decimal import Decimal, ROUND_DOWN
import math


def _positive(value, name):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"Invalid {name}: expected a finite positive number")
    return number


def normalize_amount(amount, precision, precision_mode, min_amount=None, max_amount=None):
    """Round amount down with CCXT precision semantics and enforce market limits."""
    amount = _positive(amount, "amount")
    if precision is None:
        raise ValueError("Exchange did not provide amount precision")
    precision = float(precision)
    if not math.isfinite(precision) or precision < 0:
        raise ValueError("Invalid exchange amount precision")

    if precision_mode == 4:  # CCXT TICK_SIZE
        if precision <= 0:
            raise ValueError("Tick-size amount precision must be positive")
        step = Decimal(str(precision))
    elif precision_mode == 2:  # CCXT DECIMAL_PLACES
        if not precision.is_integer():
            raise ValueError("Decimal-place precision must be an integer")
        step = Decimal(10) ** -int(precision)
    elif precision_mode == 3:  # CCXT SIGNIFICANT_DIGITS
        if not precision.is_integer() or precision <= 0:
            raise ValueError("Significant-digit precision must be a positive integer")
        exponent = math.floor(math.log10(amount)) - int(precision) + 1
        step = Decimal(10) ** exponent
    else:
        raise ValueError(f"Unsupported CCXT precision mode: {precision_mode}")

    rounded = (
        Decimal(str(amount)) / step
    ).to_integral_value(rounding=ROUND_DOWN) * step
    result = float(rounded)
    if not math.isfinite(result):
        raise ValueError("Calculated order amount is not finite")
    if min_amount is not None:
        minimum = float(min_amount)
        if not math.isfinite(minimum) or minimum < 0:
            raise ValueError("Invalid exchange minimum amount")
        # Prefer the explicit venue minimum when truncation also reaches zero.
        if minimum > 0 and result < minimum:
            raise ValueError(f"Amount {result} is below exchange minimum {min_amount}")
    if result <= 0:
        raise ValueError("Calculated order amount is below exchange precision")
    if max_amount is not None:
        maximum = float(max_amount)
        if not math.isfinite(maximum) or maximum < 0:
            raise ValueError("Invalid exchange maximum amount")
        # A zero max is an unset limit, not a zero-sized market.
        if maximum > 0 and result > maximum:
            raise ValueError(f"Amount {result} exceeds exchange maximum {max_amount}")
    return result


def validate_instrument(market_type, market):
    """Extract validated, normalized CCXT market metadata for Spot or Futures."""
    if market_type not in ("spot", "futures"):
        raise ValueError(f"Unsupported market type: {market_type}")
    amount_precision = (market.get("precision") or {}).get("amount")
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}

    if market_type == "spot":
        if not (market.get("spot") or market.get("type") == "spot"):
            raise ValueError("Selected market is not a supported Spot instrument")
        contract_type = "spot"
        contract_size = 1.0
    else:
        if not market.get("contract") or not (
            market.get("swap") or market.get("future")
        ):
            raise ValueError("Selected market is not a supported Futures contract")
        linear = bool(market.get("linear"))
        inverse = bool(market.get("inverse"))
        if linear == inverse:
            raise ValueError("Futures contract must be explicitly linear or inverse")
        contract_type = "linear" if linear else "inverse"
        contract_size = _positive(market.get("contractSize"), "contractSize")

    if amount_precision is None:
        raise ValueError("Exchange did not publish amount precision for this market")
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
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    direction = 1.0 if side == "LONG" else -1.0
    if contract_type == "inverse":
        # Convert inverse settlement-currency P&L into quote currency.
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
