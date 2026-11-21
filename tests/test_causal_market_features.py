from __future__ import annotations
from dataclasses import dataclass
import pytest
from backend.causal_market_features import wilder_atr, validate_mark_price_alignment, mark_price_liquidation_breach

@dataclass
class Bar:
    open_time_ms:int
    close_time_ms:int
    open:float
    high:float
    low:float
    close:float

def b(i,o=10,h=11,l=9,c=10):
    t=i*900_000
    return Bar(t,t+899_999,o,h,l,c)

def test_wilder_atr_seed_and_recursive_update():
    rows=[b(i,10,12,9,11) for i in range(16)]
    result=wilder_atr(rows,14)
    assert result[:13]==[None]*13
    assert result[13]==pytest.approx(3.0)
    assert result[14]==pytest.approx(3.0)
    assert result[15]==pytest.approx(3.0)

def test_true_range_uses_previous_close_for_gap():
    result=wilder_atr([b(0,10,11,9,10),b(1,14,15,13,14)],2)
    assert result==[None,pytest.approx(3.5)]

def test_atr_is_causal_future_append_does_not_change_past():
    prefix=[b(i,10,11+i*.01,9,10) for i in range(20)]
    first=wilder_atr(prefix,14)
    extended=wilder_atr(prefix+[b(20,10,1000,1,500)],14)
    assert extended[:len(prefix)]==pytest.approx(first)

def test_atr_rejects_bad_period_partial_and_duplicate_bars():
    with pytest.raises(ValueError,match="positive integer"): wilder_atr([b(0)],True)
    with pytest.raises(ValueError,match="complete"): wilder_atr([Bar(0,100,10,11,9,10)])
    with pytest.raises(ValueError,match="strictly increasing"): wilder_atr([b(0),b(0)])

def test_mark_price_requires_exact_timestamp_coverage():
    trade=[b(0),b(1),b(2)]
    assert validate_mark_price_alignment(trade,trade)["matched_bars"]==3
    with pytest.raises(ValueError,match="coverage mismatch"):
        validate_mark_price_alignment(trade,trade[:2])

def test_mark_price_long_low_and_short_high_trigger_liquidation():
    assert mark_price_liquidation_breach(side="LONG",mark_bars=[b(0,10,11,8.9,10)],liquidation_price=9)["status"]=="BREACH"
    assert mark_price_liquidation_breach(side="SHORT",mark_bars=[b(0,10,11.1,9,10)],liquidation_price=11)["status"]=="BREACH"
    assert mark_price_liquidation_breach(side="LONG",mark_bars=[b(0)],liquidation_price=8)["status"]=="CLEAR"

def test_liquidation_guard_rejects_invalid_inputs():
    with pytest.raises(ValueError,match="side"):
        mark_price_liquidation_breach(side="FLAT",mark_bars=[b(0)],liquidation_price=9)
    with pytest.raises(ValueError,match="liquidation_price"):
        mark_price_liquidation_breach(side="LONG",mark_bars=[b(0)],liquidation_price=float("nan"))
