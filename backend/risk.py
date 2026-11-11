from dataclasses import dataclass
import math

from .config import settings
from .contracts import contract_notional, contract_risk_amount, normalize_amount
from .metrics import trading_cost_rate


@dataclass
class RiskDecision:
    approved: bool
    quantity: float
    notional: float
    risk_amount: float
    reason: str
    sizing_reason: str = "risk_target"


class RiskManager:
    def __init__(self):
        self.risk_pct = settings.risk_per_trade_pct
        self.max_open_risk_pct = settings.max_open_risk_pct
        self.max_daily_loss_pct = settings.max_daily_loss_pct
        self.max_notional_pct = settings.max_position_notional_pct

    def size(self, equity, entry, stop, open_risk=0, daily_pnl=0, daily_start_equity=None,
             instrument=None):
        values = (equity, entry, stop, open_risk, daily_pnl)
        if any(not isinstance(value, (int, float)) for value in values):
            return RiskDecision(False, 0, 0, 0, "Risk inputs must be numeric")
        if any(not math.isfinite(value) for value in values):
            return RiskDecision(False, 0, 0, 0, "Risk inputs must be finite")
        if equity <= 0 or entry <= 0 or stop <= 0 or entry == stop or open_risk < 0:
            return RiskDecision(False, 0, 0, 0, "Invalid equity, entry, stop, or open risk")
        if daily_start_equity is not None:
            if not math.isfinite(daily_start_equity) or daily_start_equity <= 0:
                return RiskDecision(False, 0, 0, 0, "Invalid daily starting equity")
            if daily_pnl <= -(daily_start_equity * self.max_daily_loss_pct / 100):
                return RiskDecision(False, 0, 0, 0, "Daily loss limit reached")

        spec = instrument or {}
        contract_size = float(spec.get("contract_size", 1.0))
        contract_type = spec.get("contract_type", "spot")
        if not math.isfinite(contract_size) or contract_size <= 0:
            return RiskDecision(False, 0, 0, 0, "Invalid exchange contract size")

        risk_target = equity * self.risk_pct / 100
        per_unit_risk = contract_risk_amount(entry, stop, 1.0, contract_size, contract_type)
        per_unit_risk += (
            contract_notional(entry, 1.0, contract_size, contract_type)
            + contract_notional(stop, 1.0, contract_size, contract_type)
        ) * trading_cost_rate()
        if per_unit_risk <= 0 or not math.isfinite(per_unit_risk):
            return RiskDecision(False, 0, 0, 0, "Invalid per-contract risk")
        raw_quantity = risk_target / per_unit_risk
        max_notional = equity * self.max_notional_pct / 100
        per_unit_notional = contract_notional(entry, 1.0, contract_size, contract_type)
        max_quantity_by_notional = max_notional / per_unit_notional
        sizing_reason = "risk_target"
        quantity = min(raw_quantity, max_quantity_by_notional)
        if quantity < raw_quantity:
            sizing_reason = "position_notional_cap"

        if contract_type != "spot":
            try:
                quantity = normalize_amount(
                    quantity, spec.get("amount_precision"), spec.get("precision_mode"),
                    spec.get("min_amount"), spec.get("max_amount"),
                )
            except ValueError as exc:
                return RiskDecision(False, 0, 0, 0, str(exc))
        else:
            # Preserve existing Spot sizing semantics; Spot quantity is base units.
            quantity = math.nextafter(quantity, 0.0) if quantity * per_unit_risk > risk_target else quantity

        notional = contract_notional(entry, quantity, contract_size, contract_type)
        if spec.get("min_cost") is not None and notional < float(spec["min_cost"]):
            return RiskDecision(False, 0, 0, 0, f"Order notional {notional:.8g} is below exchange minimum {spec['min_cost']}")
        if spec.get("max_cost") is not None and notional > float(spec["max_cost"]):
            return RiskDecision(False, 0, 0, 0, f"Order notional {notional:.8g} exceeds exchange maximum {spec['max_cost']}")
        risk_amount = quantity * per_unit_risk
        if risk_amount > risk_target:
            return RiskDecision(False, 0, 0, risk_amount, "Rounded contract amount exceeds risk budget")
        if open_risk + risk_amount > equity * self.max_open_risk_pct / 100:
            return RiskDecision(False, 0, 0, risk_amount, "Maximum open risk would be exceeded after costs")
        return RiskDecision(True, quantity, notional, risk_amount, "Approved by risk limits", sizing_reason)


risk_manager = RiskManager()
