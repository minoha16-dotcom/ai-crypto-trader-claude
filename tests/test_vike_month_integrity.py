from __future__ import annotations
import csv, hashlib, json, shutil
from pathlib import Path
import pytest
from scripts import vike_month_integrity as v

def _hash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def _fixture(root, missing_day=None):
    root.mkdir(parents=True, exist_ok=True)
    start,end=v.bounds("2024-02")
    for stream,headers in v.HEADERS.items():
        p=root/f"BTCUSDT-{stream}.csv"
        with p.open("w",newline="",encoding="utf-8") as f:
            w=csv.writer(f); w.writerow(headers)
            if stream in ("trade_15m","mark_15m"):
                for ts in range(start,end,v.BAR_MS):
                    if stream=="trade_15m" and missing_day is not None and start+missing_day*86400000<=ts<start+(missing_day+1)*86400000: continue
                    if stream=="trade_15m": w.writerow([ts,"100","102","99","101","1",ts+v.CLOSE_MS,"100","10","0.5","50","0"])
                    else: w.writerow([ts,"100","102","99","101","0",ts+v.CLOSE_MS,"0","0","0","0","0"])
            else:
                for ts in (start+1,start+8*3600000,start+16*3600000): w.writerow([ts,"0.0001","100"])
    cp={"schema_version":3,"symbol":"BTCUSDT","start_ms":start,"end_ms_exclusive":end,"streams":{}}
    for stream in v.STREAMS:
        p=root/f"BTCUSDT-{stream}.csv"
        with p.open(newline="",encoding="utf-8") as f: count=sum(1 for _ in csv.DictReader(f))
        cp["streams"][stream]={"complete":True,"rows_written":count,"sha256":_hash(p)}
    (root/"BTCUSDT-checkpoint.json").write_text(json.dumps(cp),encoding="utf-8")
    return root

def test_locked_holdout_month_is_rejected():
    with pytest.raises(ValueError,match="holdout is locked"): v.bounds("2026-05")

def test_incomplete_price_day_is_excluded_whole():
    folder=_fixture(Path("tests/.tmp-vike-month"),missing_day=3)
    report,_=v.audit_month(folder,"2024-02",["BTCUSDT"])
    assert report["collection_integrity_status"]=="PASS"
    assert report["symbols"][0]["daily_audit"][3]["price_data_status"]=="EXCLUDE_DAY"
    assert report["symbols"][0]["daily_audit"][3]["trade_missing_bars"]==96
    assert report["funding_coverage_status"]=="NOT_VERIFIED"
    assert all(not d["label_eligible"] for d in report["symbols"][0]["daily_audit"])
    shutil.rmtree(folder)

def test_manifest_copy_detects_tampering(tmp_path):
    source=_fixture(tmp_path/"source")
    report,records=v.audit_month(source,"2024-02",["BTCUSDT"])
    assert report["collection_integrity_status"]=="PASS"
    report_path=source/"month-audit.json"
    report_path.write_text(json.dumps(report,indent=2,sort_keys=True)+"\\n",encoding="utf-8")
    records.append({"path":report_path.name,"bytes":report_path.stat().st_size,"sha256":_hash(report_path)})
    manifest={"month_utc":"2024-02","files":records}
    mp=source/"manifest.json"; mp.write_text(json.dumps(manifest,indent=2)+"\\n",encoding="utf-8")
    mp.with_suffix(".json.sha256").write_text(f"{_hash(mp)}  manifest.json\\n",encoding="ascii")
    destination=tmp_path/"drive"; shutil.copytree(source,destination)
    assert v.verify_copy(source,destination,mp)["status"]=="PASS_LOCAL_SYNC_PATH_COPY"
    (destination/"BTCUSDT-trade_15m.csv").write_text("tampered",encoding="utf-8")
    assert v.verify_copy(source,destination,mp)["status"]=="FAIL"

def test_checkpoint_hash_mismatch_fails_closed(tmp_path):
    folder=_fixture(tmp_path/"month")
    with (folder/"BTCUSDT-mark_15m.csv").open("a",encoding="utf-8") as f: f.write("tamper\\n")
    report,_=v.audit_month(folder,"2024-02",["BTCUSDT"])
    assert report["collection_integrity_status"]=="FAIL"
    assert any("SHA-256" in e for e in report["fatal_errors"])
