import pytest
from backend.indicators import ema, rsi, analyze
from backend.risk import RiskManager

def test_ema_constant_series():
    assert ema([10.0]*30,20)==10.0

def test_rsi_rising_series():
    assert rsi([float(x) for x in range(20,40)],14)==100.0

def test_analyze_returns_signal_schema():
    candles=[]
    for i in range(120):
        close=100+i*0.5
        candles.append([i,close-0.2,close+1,close-1,close,1000])
    result=analyze(candles)
    assert result['signal'] in ('LONG','SHORT','WAIT')
    assert result['atr14']>0

def test_risk_caps_notional_and_quantity():
    rm=RiskManager()
    d=rm.size(10000,100,95)
    assert d.approved
    assert d.risk_amount<=50
    assert d.notional<=2000

def test_risk_rejects_excess_open_risk():
    rm=RiskManager()
    d=rm.size(10000,100,95,open_risk=200)
    assert not d.approved

def test_risk_rejects_daily_loss_limit():
    rm=RiskManager()
    d=rm.size(10000,100,95,daily_pnl=-300,daily_start_equity=10000)
    assert not d.approved
