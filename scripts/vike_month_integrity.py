#!/usr/bin/env python3
"""Vike monthly local integrity audit, manifest generation and copy verification.

Funding coverage and Google Drive cloud-sync completion are never inferred here.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math, re
from datetime import datetime, timezone
from pathlib import Path

BAR_MS=900_000
CLOSE_MS=BAR_MS-1
STREAMS=("trade_15m","mark_15m","funding")
HEADERS={
 "trade_15m":["open_time","open","high","low","close","volume","close_time","quote_volume","trade_count","taker_buy_base","taker_buy_quote","ignore"],
 "mark_15m":["open_time","mark_open","mark_high","mark_low","mark_close","ignored_5","close_time","ignored_7","ignored_8","ignored_9","ignored_10","ignored_11"],
 "funding":["funding_time","funding_rate","mark_price"],
}
def digest(p):
 h=hashlib.sha256()
 with p.open("rb") as f:
  for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
 return h.hexdigest()
def bounds(month):
 if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",month): raise ValueError("month must be YYYY-MM")
 y,m=map(int,month.split("-"))
 if (y,m)<(2023,11) or (y,m)>(2026,4): raise ValueError("Only 2023-11 through 2026-04 are allowed; holdout is locked")
 a=datetime(y,m,1,tzinfo=timezone.utc)
 b=datetime(y+1,1,1,tzinfo=timezone.utc) if m==12 else datetime(y,m+1,1,tzinfo=timezone.utc)
 return int(a.timestamp()*1000),int(b.timestamp()*1000)
def read_rows(path,stream):
 with path.open(newline="",encoding="utf-8-sig") as f:
  r=csv.DictReader(f)
  if r.fieldnames!=HEADERS[stream]: raise ValueError(f"{path.name}: schema mismatch")
  return list(r)
def audit_bar_rows(rows,stream,start,end):
 out={}; invalid=[]
 names=("open","high","low","close") if stream=="trade_15m" else ("mark_open","mark_high","mark_low","mark_close")
 for line,r in enumerate(rows,2):
  try:
   ts=int(r["open_time"]); ct=int(r["close_time"]); o,h,l,c=[float(r[n]) for n in names]
   valid=start<=ts<end and ts%BAR_MS==0 and ct==ts+CLOSE_MS and all(math.isfinite(v) and v>0 for v in (o,h,l,c)) and h>=max(o,l,c) and l<=min(o,c)
   if ts in out: invalid.append({"line":line,"timestamp_ms":ts,"reason":"duplicate"}); out[ts]=False
   else: out[ts]=valid
   if not valid: invalid.append({"line":line,"timestamp_ms":ts,"reason":"invalid_bar"})
  except Exception: invalid.append({"line":line,"reason":"malformed_bar"})
 return out,invalid
def audit_funding(rows,start,end):
 ts=[]; bad=[]
 for line,r in enumerate(rows,2):
  try:
   t=int(r["funding_time"]); rate=float(r["funding_rate"]); mark=float(r["mark_price"])
   if not start<=t<end or not math.isfinite(rate) or not math.isfinite(mark) or mark<=0: raise ValueError("invalid funding row")
   ts.append(t)
  except Exception as e: bad.append({"line":line,"reason":str(e)})
 ordered=all(a<b for a,b in zip(ts,ts[1:]))
 return {"rows":len(rows),"valid_rows":len(ts),"invalid_rows":bad,"strictly_increasing":ordered,"structural_status":"PASS" if ts and not bad and ordered else "FAIL","coverage_status":"NOT_VERIFIED"}
def audit_month(folder,month,symbols):
 start,end=bounds(month); ndays=(end-start)//86_400_000; reports=[]; errors=[]; records=[]
 for sym in symbols:
  cp=folder/f"{sym}-checkpoint.json"
  try:
   if not cp.is_file(): raise ValueError("checkpoint missing")
   state=json.loads(cp.read_text(encoding="utf-8"))
   if (state.get("schema_version"),state.get("symbol"),state.get("start_ms"),state.get("end_ms_exclusive"))!=(3,sym,start,end): raise ValueError("checkpoint scope/schema mismatch")
   rows={}; meta={}
   for stream in STREAMS:
    ss=state.get("streams",{}).get(stream)
    if not isinstance(ss,dict) or ss.get("complete") is not True: raise ValueError(f"{stream}: checkpoint incomplete")
    p=folder/f"{sym}-{stream}.csv"
    if not p.is_file(): raise ValueError(f"{stream}: CSV missing")
    sh=digest(p)
    if ss.get("sha256")!=sh: raise ValueError(f"{stream}: checkpoint SHA-256 mismatch")
    rows[stream]=read_rows(p,stream)
    if len(rows[stream])!=ss.get("rows_written"): raise ValueError(f"{stream}: row count mismatch")
    meta[stream]={"file":p.name,"bytes":p.stat().st_size,"rows":len(rows[stream]),"sha256":sh}
    records.append({"path":p.name,"bytes":p.stat().st_size,"sha256":sh})
   records.append({"path":cp.name,"bytes":cp.stat().st_size,"sha256":digest(cp)})
   tm,ti=audit_bar_rows(rows["trade_15m"],"trade_15m",start,end)
   mm,mi=audit_bar_rows(rows["mark_15m"],"mark_15m",start,end)
   fd=audit_funding(rows["funding"],start,end)
   daily=[]
   for i in range(ndays):
    ds=start+i*86_400_000; de=ds+86_400_000; expected=set(range(ds,de,BAR_MS))
    tv={t for t,ok in tm.items() if ds<=t<de and ok}; mv={t for t,ok in mm.items() if ds<=t<de and ok}
    tb=sum(1 for x in ti if ds<=x.get("timestamp_ms",-1)<de); mb=sum(1 for x in mi if ds<=x.get("timestamp_ms",-1)<de)
    ok=len(tv)==96 and len(mv)==96 and not (expected-tv) and not (expected-mv) and not tb and not mb
    daily.append({"date_utc":datetime.fromtimestamp(ds/1000,timezone.utc).date().isoformat(),"expected_bars_per_feed":96,"trade_valid_bars":len(tv),"mark_valid_bars":len(mv),"trade_missing_bars":len(expected-tv),"mark_missing_bars":len(expected-mv),"trade_invalid_rows":tb,"mark_invalid_rows":mb,"price_data_status":"ELIGIBLE" if ok else "EXCLUDE_DAY","funding_coverage_status":"NOT_VERIFIED","label_eligible":False,"exclusion_reason":None if ok else "Incomplete/invalid/duplicate/misaligned Trade or Mark day"})
   reports.append({"symbol":sym,"checkpoint_status":"PASS","streams":meta,"funding":fd,"daily_audit":daily,"price_eligible_days":sum(x["price_data_status"]=="ELIGIBLE" for x in daily),"excluded_price_days":sum(x["price_data_status"]!="ELIGIBLE" for x in daily),"label_eligible_days":0})
  except Exception as e: errors.append(f"{sym}: {e}")
 return {"schema_version":1,"month_utc":month,"start_ms":start,"end_ms_exclusive":end,"symbols":reports,"fatal_errors":errors,"collection_integrity_status":"PASS" if len(reports)==len(symbols) and not errors else "FAIL","funding_coverage_status":"NOT_VERIFIED","model_or_label_approval":"NOT_GRANTED","missing_price_day_policy":"EXCLUDE_WHOLE_UTC_DAY; no fill or interpolation","holdout_policy":"2026-05 through 2026-10 is locked"},records
def cmd_audit(a):
 symbols=[x.strip().upper() for x in a.symbols.split(",") if x.strip()]
 if not symbols or len(set(symbols))!=len(symbols) or any(not x.isalnum() for x in symbols): raise ValueError("invalid symbols")
 report,records=audit_month(a.month_dir,a.month,symbols)
 a.report.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8")
 if report["collection_integrity_status"]!="PASS": raise ValueError("integrity audit failed; refusing manifest")
 records.append({"path":a.report.name,"bytes":a.report.stat().st_size,"sha256":digest(a.report)})
 manifest={"schema_version":1,"dataset":"Vike V3.1 Binance USD-M monthly raw staging","month_utc":a.month,"symbols":symbols,"streams":list(STREAMS),"start_ms":report["start_ms"],"end_ms_exclusive":report["end_ms_exclusive"],"collection_integrity_status":"PASS","funding_coverage_status":"NOT_VERIFIED","model_or_label_approval":"NOT_GRANTED","files":records}
 a.manifest.write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
 a.manifest.with_suffix(a.manifest.suffix+".sha256").write_text(f"{digest(a.manifest)}  {a.manifest.name}\n",encoding="ascii")
 print(json.dumps({"status":"PASS_LOCAL_INTEGRITY","month":a.month,"files":len(records),"funding_coverage_status":"NOT_VERIFIED","model_or_label_approval":"NOT_GRANTED"},indent=2))
def cmd_verify(a):
 m=json.loads(a.manifest.read_text(encoding="utf-8")); errors=[]; verified=[]
 side=a.manifest.with_suffix(a.manifest.suffix+".sha256")
 if not side.is_file() or side.read_text(encoding="ascii").split()[0].lower()!=digest(a.manifest): errors.append("manifest SHA-256 sidecar missing/mismatch")
 for item in m.get("files",[]):
  s=a.source/item["path"]; d=a.destination/item["path"]
  if not s.is_file() or not d.is_file(): errors.append("missing source/destination: "+item["path"]); continue
  if s.stat().st_size!=item["bytes"] or digest(s)!=item["sha256"]: errors.append("source differs from manifest: "+item["path"])
  if d.stat().st_size!=item["bytes"] or digest(d)!=item["sha256"]: errors.append("destination SHA-256 mismatch: "+item["path"])
  else: verified.append(item["path"])
 for name in (a.manifest.name,side.name):
  s=a.source/name; d=a.destination/name
  if not s.is_file() or not d.is_file() or digest(s)!=digest(d): errors.append("manifest copy missing/mismatch: "+name)
 result={"month_utc":m.get("month_utc"),"files_expected":len(m.get("files",[]))+2,"files_verified":len(verified)+ (0 if errors else 2),"verified_files":verified,"status":"PASS_LOCAL_SYNC_PATH_COPY" if not errors else "FAIL","cloud_sync_status":"NOT_VERIFIED_BY_LOCAL_HASH_CHECK","errors":errors}
 print(json.dumps(result,indent=2))
 if errors: raise SystemExit(2)
def main():
 p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="cmd",required=True)
 a=sub.add_parser("audit"); a.add_argument("--month",required=True); a.add_argument("--month-dir",type=Path,required=True); a.add_argument("--symbols",required=True); a.add_argument("--report",type=Path,required=True); a.add_argument("--manifest",type=Path,required=True)
 v=sub.add_parser("verify-copy"); v.add_argument("--source",type=Path,required=True); v.add_argument("--destination",type=Path,required=True); v.add_argument("--manifest",type=Path,required=True)
 args=p.parse_args()
 try: cmd_audit(args) if args.cmd=="audit" else cmd_verify(args)
 except Exception as e: print("ERROR:",e); return 2
 return 0
if __name__=="__main__": raise SystemExit(main())
