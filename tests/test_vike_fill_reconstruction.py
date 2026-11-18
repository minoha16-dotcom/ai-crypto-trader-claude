from datetime import datetime, timezone
from backend.vike_reconstruction import reconstruct_vike_fills

def f(side="buy", size=1, price=100, before=0, pnl=0, fee=.1, tid="1",
      wallet="0xabc", second=1, token="USDC"):
    return {"fill_ts":datetime(2026,11,11,0,0,second,tzinfo=timezone.utc),
            "wallet":wallet,"asset":"BTC","side":side,"size_token":size,
            "price":price,"start_position":before,"closed_pnl":pnl,
            "fee":fee,"fee_token":token,"trade_id":tid}

def test_long_round_trip_net_of_fees():
    r=reconstruct_vike_fills([f(size=2),f("sell",2,110,2,20,.3,"2",second=2)])
    p=r["closed_positions"][0]
    assert p["side"]=="LONG" and p["realized_pnl_usd"]==20
    assert round(p["net_realized_pnl_usd"],8)==19.6
    assert p["reconstruction_status"]=="COMPLETE"

def test_short_round_trip():
    r=reconstruct_vike_fills([f("sell",1,100,0,0,.1,"1"),f("buy",1,90,-1,10,.1,"2",second=2)])
    assert r["closed_positions"][0]["side"]=="SHORT"
    assert r["closed_positions"][0]["reconstruction_status"]=="COMPLETE"

def test_add_and_partial_reduction():
    r=reconstruct_vike_fills([f(size=1,price=100),f("buy",1,120,1,0,.1,"2",second=2),
        f("sell",1,120,2,10,.1,"3",second=3),f("sell",1,130,1,10,.1,"4",second=4)])
    p=r["closed_positions"][0]
    assert p["entry_price"]==110 and p["entry_quantity"]==2
    assert p["realized_pnl_usd"]==20 and p["reconstruction_status"]=="COMPLETE"

def test_reversal_splits_old_and_new_lifecycles():
    r=reconstruct_vike_fills([f(),f("sell",2,90,1,-10,.2,"2",second=2),
        f("buy",1,80,-1,10,.1,"3",second=3)])
    assert [p["side"] for p in r["closed_positions"]]==["LONG","SHORT"]
    assert all(p["reconstruction_status"]=="COMPLETE" for p in r["closed_positions"])

def test_carry_in_close_is_incomplete():
    r=reconstruct_vike_fills([f("sell",1,90,1,-10,.1,"1")])
    p=r["closed_positions"][0]
    assert p["reconstruction_status"]=="INCOMPLETE_CARRY_IN"
    assert p["entry_price"] is None

def test_mismatch_and_unsupported_fee_fail_closed():
    r=reconstruct_vike_fills([f(token="ETH"),f("sell",2,110,2,10,.1,"2",second=2)])
    p=r["closed_positions"][0]
    assert p["reconstruction_status"]=="INCOMPLETE_ACCOUNTING"
    assert "START_POSITION_MISMATCH" in p["issues"]
    assert "UNSUPPORTED_FEE_CURRENCY" in p["issues"]

def test_deduplicate_only_same_wallet_fill():
    x=f()
    r=reconstruct_vike_fills([x,x,f(side="sell",before=0,tid="1",wallet="0xdef")])
    assert r["fills_seen"]==2
    assert r["diagnostics"]["errors"]["DUPLICATE_WALLET_FILL"]==1
