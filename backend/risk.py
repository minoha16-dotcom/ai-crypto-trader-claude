from dataclasses import dataclass
from .config import settings


@dataclass
class RiskDecision:
    approved: bool
    quantity: float
    notional: float
    risk_amount: float
    reason: str


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
        risk_amount = equity * self.risk_pct / 100
        if open_risk + risk_amount > equity * self.max_open_risk_pct / 100:
            return RiskDecision(False, 0, 0, risk_amount, "Maximum open risk would be exceeded")
        quantity = risk_amount / abs(entry - stop)
        max_notional = equity * self.max_notional_pct / 100
        notional = quantity * entry
        if notional > max_notional:
            quantity = max_notional / entry
            notional = max_notional
            risk_amount = quantity * abs(entry - stop)
        return RiskDecision(True, quantity, notional, risk_amount, "Approved by risk limits")


risk_manager = RiskManager()
