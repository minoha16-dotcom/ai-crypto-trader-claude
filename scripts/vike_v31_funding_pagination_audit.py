#!/usr/bin/env python3
"""Read-only Binance Funding pagination provenance audit.

Fetches public Binance USD-M funding history and compares it with the frozen
local Funding CSV archive. Writes request/page metadata, exact response bytes,
hashes and a review report only under derived/funding-audit. Never writes to
raw-staging and never certifies itself as VERIFIED.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.vike_exploratory_train import (
    MONTHS, SYMBOLS, month_bounds, resolve_csv, sha256_file,
)

BASE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
PAGE_LIMIT = 1000
USER_AGENT = "Vike-V3.1-Research-Funding-Audit/1.0"
FLOAT_ABS_TOLERANCE = 1e-12


def read_local_funding(path: Path, start_ms: int, end_ms: int) -> dict[int, tuple[float, float]]:
    rows: dict[int, tuple[float, float]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"funding_time", "funding_rate", "mark_price"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path}: missing required Funding columns")
        for line, row in enumerate(reader, 2):
            try:
                ts = int(row["funding_time"])
                rate, mark = float(row["funding_rate"]), float(row["mark_price"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{path}:{line}: malformed local Funding row") from exc
            if not start_ms <= ts < end_ms or not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise ValueError(f"{path}:{line}: invalid/out-of-window local Funding row")
            if ts in rows:
                raise ValueError(f"{path}:{line}: duplicate local fundingTime {ts}")
            rows[ts] = (rate, mark)
    return rows


def request_page(symbol: str, start_ms: int, end_inclusive_ms: int) -> tuple[bytes, list[dict[str, Any]], dict]:
    params = {
        "symbol": symbol,
        "startTime": str(start_ms),
        "endTime": str(end_inclusive_ms),
        "limit": str(PAGE_LIMIT),
    }
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            status = response.status
            headers = {
                "date": response.headers.get("Date"),
                "content_type": response.headers.get("Content-Type"),
                "x_mbx_used_weight_1m": response.headers.get("X-MBX-USED-WEIGHT-1M"),
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"Binance HTTP {exc.code} for {symbol}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Binance request failed for {symbol}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Binance returned non-JSON response for {symbol}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"Binance API error for {symbol}: {payload!r}")
    for item in payload:
        if not isinstance(item, dict) or not {"symbol", "fundingTime", "fundingRate", "markPrice"}.issubset(item):
            raise RuntimeError(f"Unexpected Binance Funding row schema for {symbol}")
        if item["symbol"] != symbol:
            raise RuntimeError(f"Binance returned unexpected symbol {item.get('symbol')!r}")
    return raw, payload, {"http_status": status, "response_headers": headers, "request_url": url}


def audit_group(symbol: str, month: str, raw_root: Path, output_root: Path,
                pause_seconds: float) -> dict:
    start_ms, end_ms = month_bounds(month)
    local_path = resolve_csv(raw_root, month, symbol, "funding")
    local = read_local_funding(local_path, start_ms, end_ms)
    page_dir = output_root / "api-responses" / symbol / month
    page_dir.mkdir(parents=True, exist_ok=True)

    remote: dict[int, tuple[float, float]] = {}
    pages = []
    cursor = start_ms
    page_number = 0
    while cursor < end_ms:
        raw, payload, metadata = request_page(symbol, cursor, end_ms - 1)
        page_number += 1
        response_sha = hashlib.sha256(raw).hexdigest()
        response_path = page_dir / f"page-{page_number:03d}.json"
        response_path.write_bytes(raw)

        timestamps = []
        for item in payload:
            try:
                ts = int(item["fundingTime"])
                rate = float(item["fundingRate"])
                mark = float(item["markPrice"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError(f"Malformed Binance Funding values: {symbol} {month}") from exc
            if not start_ms <= ts < end_ms or ts < cursor:
                raise RuntimeError(f"Binance returned out-of-range/non-monotonic timestamp: {symbol} {month}")
            if ts in remote:
                raise RuntimeError(f"Duplicate Binance fundingTime across pages: {symbol} {month} {ts}")
            if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise RuntimeError(f"Invalid Binance Funding numeric value: {symbol} {month} {ts}")
            remote[ts] = (rate, mark)
            timestamps.append(ts)

        if timestamps != sorted(timestamps):
            raise RuntimeError(f"Binance page timestamps are not ascending: {symbol} {month}")
        pages.append({
            "page": page_number,
            "request": {
                "endpoint": BASE_URL,
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": PAGE_LIMIT,
            },
            "http_status": metadata["http_status"],
            "response_headers": metadata["response_headers"],
            "row_count": len(payload),
            "first_funding_time_ms": timestamps[0] if timestamps else None,
            "last_funding_time_ms": timestamps[-1] if timestamps else None,
            "response_sha256": response_sha,
            "response_file": str(response_path.relative_to(output_root)),
            "response_bytes": len(raw),
        })
        if len(payload) < PAGE_LIMIT:
            break
        if not timestamps:
            raise RuntimeError(f"Empty full page is impossible: {symbol} {month}")
        next_cursor = timestamps[-1] + 1
        if next_cursor <= cursor:
            raise RuntimeError(f"Pagination cursor did not advance: {symbol} {month}")
        cursor = next_cursor
        time.sleep(pause_seconds)

    remote_ts, local_ts = set(remote), set(local)
    missing_local = sorted(remote_ts - local_ts)
    extra_local = sorted(local_ts - remote_ts)
    mismatches = []
    for ts in sorted(remote_ts & local_ts):
        rr, rm = remote[ts]
        lr, lm = local[ts]
        if not math.isclose(rr, lr, rel_tol=0.0, abs_tol=FLOAT_ABS_TOLERANCE) or not math.isclose(
            rm, lm, rel_tol=0.0, abs_tol=FLOAT_ABS_TOLERANCE
        ):
            mismatches.append({
                "funding_time_ms": ts,
                "binance_rate": rr, "local_rate": lr,
                "binance_mark_price": rm, "local_mark_price": lm,
            })

    passed = not missing_local and not extra_local and not mismatches and bool(remote)
    return {
        "symbol": symbol,
        "month": month,
        "status": "PASS" if passed else "FAIL",
        "local_file": str(local_path),
        "local_file_sha256": sha256_file(local_path),
        "local_rows": len(local),
        "binance_rows": len(remote),
        "missing_local_timestamps_count": len(missing_local),
        "missing_local_timestamps_sample": missing_local[:20],
        "extra_local_timestamps_count": len(extra_local),
        "extra_local_timestamps_sample": extra_local[:20],
        "value_mismatch_count": len(mismatches),
        "value_mismatch_sample": mismatches[:20],
        "binance_first_funding_time_ms": min(remote) if remote else None,
        "binance_last_funding_time_ms": max(remote) if remote else None,
        "page_count": len(pages),
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-root", required=True, type=Path,
                        help="Vike V3.1 Drive root containing raw-staging and derived/")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--pause-seconds", type=float, default=0.15)
    args = parser.parse_args()

    drive_root = args.drive_root.expanduser().resolve()
    raw_root = (drive_root / "raw-staging").resolve()
    output_root = (drive_root / "derived" / "funding-audit" / "binance-pagination").resolve()
    if not raw_root.is_dir():
        parser.error(f"raw-staging directory not found: {raw_root}")
    if output_root == raw_root or raw_root in output_root.parents:
        parser.error("Output directory must be outside raw-staging")
    if not math.isfinite(args.pause_seconds) or args.pause_seconds < 0:
        parser.error("--pause-seconds must be finite and nonnegative")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in SYMBOLS for s in symbols):
        parser.error("symbols must be a unique subset of the frozen eight-symbol universe")

    output_root.mkdir(parents=True, exist_ok=True)
    records = []
    errors = []
    for symbol in symbols:
        for month in MONTHS:
            try:
                print(f"[AUDIT] {symbol} {month}", flush=True)
                records.append(audit_group(symbol, month, raw_root, output_root, args.pause_seconds))
            except Exception as exc:
                errors.append({"symbol": symbol, "month": month, "error": str(exc)})
                print(f"[ERROR] {symbol} {month}: {exc}", flush=True)

    passed = sum(row["status"] == "PASS" for row in records)
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "venue": "Binance USD-M",
        "endpoint": BASE_URL,
        "method": "PUBLIC_READ_ONLY_FUNDING_HISTORY_PAGINATION",
        "symbols": symbols,
        "months": list(MONTHS),
        "expected_symbol_month_groups": len(symbols) * len(MONTHS),
        "audited_symbol_month_groups": len(records),
        "passed_symbol_month_groups": passed,
        "failed_symbol_month_groups": len(records) - passed,
        "errors": errors,
        "raw_staging_mutated": False,
        "holdout_months_accessed": False,
        "coverage_status": "PENDING_INDEPENDENT_REVIEW",
        "records": records,
        "review_requirements": [
            "Review request bounds, page counts, response hashes and exact archived response bytes.",
            "Confirm every response page is retained and its SHA-256 matches this report.",
            "Confirm Binance API semantics and local archive provenance independently.",
            "Do not change coverage_status to VERIFIED automatically; the report is evidence, not a self-approval.",
        ],
    }
    report_path = output_root / "funding-pagination-provenance.json"
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "report_path": str(report_path),
        "response_archive_dir": str(output_root / "api-responses"),
        "groups_expected": report["expected_symbol_month_groups"],
        "groups_audited": report["audited_symbol_month_groups"],
        "groups_passed": passed,
        "groups_failed": report["failed_symbol_month_groups"],
        "errors": len(errors),
        "coverage_status": report["coverage_status"],
        "raw_staging_mutated": False,
    }, indent=2))
    return 0 if not errors and len(records) == len(symbols) * len(MONTHS) and passed == len(records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
