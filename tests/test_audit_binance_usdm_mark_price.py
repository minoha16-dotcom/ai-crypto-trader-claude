import csv
from scripts.audit_binance_usdm_mark_price import audit_symbol_window,TRADE_HEADERS,MARK_HEADERS

def write(path,headers,rows):
 with path.open("w",newline="") as f:
  w=csv.writer(f);w.writerow(headers);w.writerows(rows)

def row(t,mark=False):
 return [t,10,11,9,10,0,t+899999,0,0,0,0,0]
def test_complete_exact_mark_coverage_passes(tmp_path):
 trade=tmp_path/"trade.csv";mark=tmp_path/"mark.csv"
 write(trade,TRADE_HEADERS,[row(0),row(900000)])
 write(mark,MARK_HEADERS,[row(0),row(900000)])
 r=audit_symbol_window(symbol="BTCUSDT",start_ms=0,end_ms=1800000,trade_csv=trade,mark_csv=mark)
 assert r["status"]=="PASS" and r["expected_bars"]==2
def test_missing_mark_candle_fails_closed(tmp_path):
 trade=tmp_path/"trade.csv";mark=tmp_path/"mark.csv"
 write(trade,TRADE_HEADERS,[row(0),row(900000)])
 write(mark,MARK_HEADERS,[row(0)])
 r=audit_symbol_window(symbol="BTCUSDT",start_ms=0,end_ms=1800000,trade_csv=trade,mark_csv=mark)
 assert r["status"]=="FAIL" and r["mark_missing_count"]==1
def test_missing_trade_candle_fails_even_if_mark_matches(tmp_path):
 trade=tmp_path/"trade.csv";mark=tmp_path/"mark.csv"
 write(trade,TRADE_HEADERS,[row(0)])
 write(mark,MARK_HEADERS,[row(0),row(900000)])
 r=audit_symbol_window(symbol="BTCUSDT",start_ms=0,end_ms=1800000,trade_csv=trade,mark_csv=mark)
 assert r["status"]=="FAIL" and r["trade_missing_count"]==1
