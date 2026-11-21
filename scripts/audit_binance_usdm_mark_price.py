"""Per-symbol, per-window Binance USD-M trade/Mark Price coverage gate."""
from __future__ import annotations
import argparse,csv,json,math
from datetime import datetime,timezone
from pathlib import Path
BAR_MS=900_000
BAR_CLOSE_OFFSET_MS=BAR_MS-1
TRADE_HEADERS=["open_time","open","high","low","close","volume","close_time","quote_volume","trade_count","taker_buy_base","taker_buy_quote","ignore"]
MARK_HEADERS=["open_time","mark_open","mark_high","mark_low","mark_close","ignored_5","close_time","ignored_7","ignored_8","ignored_9","ignored_10","ignored_11"]

def parse_ms(s):
 d=datetime.fromisoformat(s.replace("Z","+00:00"))
 if d.tzinfo is None: raise ValueError("timestamps must include UTC offset")
 return int(d.astimezone(timezone.utc).timestamp()*1000)

def _read(path,headers,prefix):
 rows=[];errors=[]
 with path.open(newline="",encoding="utf-8-sig") as f:
  reader=csv.DictReader(f)
  if reader.fieldnames!=headers: raise ValueError(f"{prefix} schema mismatch: {reader.fieldnames}")
  for line,r in enumerate(reader,2):
   try:
    ts=int(r["open_time"]);close_ts=int(r["close_time"])
    names=("open","high","low","close") if prefix=="trade" else ("mark_open","mark_high","mark_low","mark_close")
    o,h,l,c=[float(r[n]) for n in names]
    if not all(math.isfinite(v) and v>0 for v in (o,h,l,c)): raise ValueError("OHLC must be finite and positive")
    if h<max(o,l,c) or l>min(o,l,c): raise ValueError("inconsistent OHLC")
    if ts%BAR_MS or close_ts!=ts+BAR_CLOSE_OFFSET_MS: raise ValueError("not a complete UTC 15m candle")
    rows.append((ts,close_ts,o,h,l,c))
   except Exception as e: errors.append({"line":line,"reason":str(e)})
 return rows,errors

def audit_symbol_window(*,symbol,start_ms,end_ms,trade_csv,mark_csv):
 if start_ms%BAR_MS or end_ms%BAR_MS or end_ms<=start_ms: raise ValueError("window must be positive and aligned to UTC 15m boundaries")
 expected=list(range(start_ms,end_ms,BAR_MS))
 trade,te=_read(Path(trade_csv),TRADE_HEADERS,"trade")
 mark,me=_read(Path(mark_csv),MARK_HEADERS,"mark")
 tt=[x[0] for x in trade]; mt=[x[0] for x in mark]
 missing_trade=sorted(set(expected)-set(tt));extra_trade=sorted(set(tt)-set(expected))
 missing_mark=sorted(set(expected)-set(mt));extra_mark=sorted(set(mt)-set(expected))
 mismatched=[t for t in set(tt)&set(mt) if trade[tt.index(t)][:2]!=mark[mt.index(t)][:2]]
 passed=not(te or me or missing_trade or extra_trade or missing_mark or extra_mark or mismatched) and len(tt)==len(expected) and len(mt)==len(expected)
 return {"symbol":symbol,"start_ms":start_ms,"end_ms_exclusive":end_ms,
 "expected_bars":len(expected),"trade_rows":len(tt),"mark_rows":len(mt),
 "trade_missing_count":len(missing_trade),"trade_extra_count":len(extra_trade),
 "mark_missing_count":len(missing_mark),"mark_extra_count":len(extra_mark),
 "timestamp_or_close_mismatches":len(mismatched),"trade_invalid_rows":te[:20],"mark_invalid_rows":me[:20],
 "first_trade_ms":tt[0] if tt else None,"last_trade_ms":tt[-1] if tt else None,
 "first_mark_ms":mt[0] if mt else None,"last_mark_ms":mt[-1] if mt else None,
 "status":"PASS" if passed else "FAIL"}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("--manifest",required=True,type=Path);p.add_argument("--start",required=True);p.add_argument("--end",required=True);p.add_argument("--out",required=True,type=Path);a=p.parse_args()
 start,end=parse_ms(a.start),parse_ms(a.end);data=json.loads(a.manifest.read_text(encoding="utf-8"))
 results=[audit_symbol_window(symbol=x["symbol"],start_ms=start,end_ms=end,trade_csv=x["trade_csv"],mark_csv=x["mark_csv"]) for x in data["symbols"]]
 report={"schema_version":1,"start_ms":start,"end_ms_exclusive":end,"symbols":results,"status":"PASS" if results and all(x["status"]=="PASS" for x in results) else "FAIL","coverage_verified":bool(results) and all(x["status"]=="PASS" for x in results)}
 a.out.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8");print(json.dumps(report,indent=2));return 0 if report["coverage_verified"] else 2
if __name__=="__main__": raise SystemExit(main())
