from dataclasses import dataclass
from .config import settings


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

    def size(self, equity, entry, stop, open_risk=0, daily_pnl=0, daily_start_equity=None):
        values = (equity, entry, stop, open_risk, daily_pnl)
        if any(not isinstance(value, (int, float)) for value in values):
            return RiskDecision(False, 0, 0, 0, "Risk inputs must be numeric")
        if any(not __import__("math").isfinite(value) for value in values):
            return RiskDecision(False, 0, 0, 0, "Risk inputs must be finite")
        if equity <= 0 or entry <= 0 or stop <= 0 or entry == stop or open_risk < 0:
            return RiskDecision(False, 0, 0, 0, "Invalid equity, entry, stop, or open risk")
        if daily_start_equity is not None:
            if not __import__("math").isfinite(daily_start_equity) or daily_start_equity <= 0:
                return RiskDecision(False, 0, 0, 0, "Invalid daily starting equity")
            if daily_pnl <= -(daily_start_equity * self.max_daily_loss_pct / 100):
                return RiskDecision(False, 0, 0, 0, "Daily loss limit reached")
        risk_target = equity * self.risk_pct / 100
        if open_risk + risk_target > equity * self.max_open_risk_pct / 100:
            return RiskDecision(False, 0, 0, risk_target, "Maximum open risk would be exceeded")
        cost_rate = float(settings.taker_fee_rate) + float(settings.slippage_bps) / 10000
        per_unit_risk = abs(entry - stop) + (entry + stop) * cost_rate
        quantity = risk_target / per_unit_risk
        # Avoid a floating-point rounding overshoot of the configured risk budget.
        if quantity * per_unit_risk > risk_target:
            import math
            quantity = math.nextafter(quantity, 0.0)
        max_notional = equity * self.max_notional_pct / 100
        notional = quantity * entry
        sizing_reason = "risk_target"
        if notional > max_notional:
            quantity = max_notional / entry
            notional = max_notional
            sizing_reason = "position_notional_cap"
        risk_amount = quantity * per_unit_risk
        if open_risk + risk_amount > equity * self.max_open_risk_pct / 100:
            return RiskDecision(False, 0, 0, risk_amount, "Maximum open risk would be exceeded after costs")
        return RiskDecision(True, quantity, notional, risk_amount, "Approved by risk limits", sizing_reason)


risk_manager = RiskManager()
