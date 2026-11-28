import math
from backend.vike_live import _context_returns, _closed, _latest_breakout, BAR_MS
from backend.ml_shadow import direction_aligned_features

def candle(open_ms, o, h, l, c, v, close_ms):
    return [open_ms,o,h,l,c,v,close_ms]

def test_forming_candle_is_excluded():
    rows=[candle(0,10,11,9,10,1,899999),candle(900000,10,11,9,10,1,1799999)]
    assert len(_closed(rows, 1_800_000)) == 2
    assert len(_closed(rows, 1_799_999)) == 1

def test_context_only_uses_closed_candle_as_of_signal():
    rows=[candle(i*HOUR,100+i,102+i,99+i,101+i,10,(i+1)*HOUR-1) for i in range(8)]
    r1,r4,close_ms=_context_returns(rows,7*HOUR-1,HOUR)
    assert close_ms <= 7*HOUR-1
    assert r1>0 and r4>0
    _, _, earlier_close = _context_returns(rows,6*HOUR-1,HOUR)
    assert earlier_close <= 6*HOUR-1

HOUR=3_600_000
def test_direction_transform_mirrors_short_and_is_finite():
    raw={"ret_1":.01,"ret_4":.02,"ret_16":.03,"atr_pct":.01,
      "donchian_width_pct":.04,"channel_position":1.1,"volume_ratio":1.5,
      "rsi14":.7,"hour_sin":0.2,"hour_cos":0.3,"htf_1h_ret_1":.01,
      "htf_1h_ret_4":.02,"htf_4h_ret_1":.03,"htf_4h_ret_4":.04}
    long=direction_aligned_features(raw,"LONG")
    short=direction_aligned_features(raw,"SHORT")
    assert long["dir_ret_1"] == -short["dir_ret_1"]
    assert long["dir_rsi14"] == 1-short["dir_rsi14"]
    assert all(math.isfinite(v) for v in long.values())

def test_event_generator_rejects_stale_signal():
    # Synthetic bars with no recent valid breakout must not return a candidate.
    bars=[]
    for i in range(80):
        price=100+math.sin(i/4)
        bars.append(candle(i*BAR_MS,price,price+1,price-1,price,100,(i+1)*BAR_MS-1))
    h1=[candle(i*HOUR,100,102,99,101,100,(i+1)*HOUR-1) for i in range(30)]
    h4=[candle(i*4*HOUR,100,102,99,101,100,(i+1)*4*HOUR-1) for i in range(30)]
    assert _latest_breakout("BTCUSDT",bars,h1,h4,80*BAR_MS) is None
