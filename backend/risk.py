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
        self.risk_pct=settings.risk_per_trade_pct; self.max_open_risk_pct=settings.max_open_risk_pct
        self.max_daily_loss_pct=settings.max_daily_loss_pct; self.max_notional_pct=settings.max_position_notional_pct
    def size(self,equity,entry,stop,open_risk=0,daily_pnl=0,daily_start_equity=None):
        if min(equity,entry,stop)<=0 or entry==stop: return RiskDecision(False,0,0,0,'Invalid equity, entry, or stop')
        if daily_start_equity and daily_pnl<=-(daily_start_equity*self.max_daily_loss_pct/100): return RiskDecision(False,0,0,0,'Daily loss limit reached')
        risk_amount=equity*self.risk_pct/100
        if open_risk+risk_amount>equity*self.max_open_risk_pct/100: return RiskDecision(False,0,0,risk_amount,'Maximum open risk would be exceeded')
        quantity=risk_amount/abs(entry-stop); max_notional=equity*self.max_notional_pct/100; notional=quantity*entry
        if notional>max_notional:
            quantity=max_notional/entry; notional=max_notional; risk_amount=quantity*abs(entry-stop)
        return RiskDecision(True,quantity,notional,risk_amount,'Approved by risk limits')
risk_manager=RiskManager()
