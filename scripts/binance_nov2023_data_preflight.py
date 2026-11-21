#!/usr/bin/env python3
"""Download and audit the Binance USD-M BTCUSDT November 2023 pilot.

This is a data preflight only. It deliberately does not create model labels or
train Advisor: the current Advisor approval path is scoped to external-wallet
outcomes, and the existing backtest does not yet consume historical funding
events plus mark-price liquidation candles as a synchronized execution feed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data.binance.vision"
API = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
INTERVAL = "15m"
START_MS = 1698796800000  # 2023-11-01T00:00:00Z
END_MS = 1701388800000    # 2023-12-01T00:00:00Z (exclusive)
BAR_MS = 15 * 60 * 1000
EXPECTED_BARS = 2880
EXPECTED_MARK_MISSING_MS = 1699587900000  # 2023-11-10T03:45:00Z
SHA256_RE = re.compile(r"(?i)\b([0-9a-f]{64})\b")


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ai-crypto-trader-vike-v31-research/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} downloading {url}")
        return response.read()


def verify_checksum(archive: bytes, checksum_text: str, filename: str) -> str:
    match = SHA256_RE.search(checksum_text)
    if not match:
        raise ValueError(f"checksum sidecar for {filename} has no SHA-256 digest")
    expected = match.group(1).lower()
    observed = sha256(archive)
    if observed != expected:
        raise ValueError(
            f"SHA-256 mismatch for {filename}: expected {expected}, observed {observed}"
        )
    return observed


def parse_archive(archive: bytes, label: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        bad = zf.testzip()
        if bad:
            raise ValueError(f"{label}: corrupt ZIP member {bad}")
        csv_names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"{label}: expected exactly one CSV, found {len(csv_names)}")
        with zf.open(csv_names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            if not reader.fieldnames or "open_time" not in reader.fieldnames:
                raise ValueError(f"{label}: CSV must contain open_time header")
            rows = list(reader)
    return rows


def audit_timestamps(
    timestamps: list[int],
    expected: set[int],
) -> dict:
    unique = set(timestamps)
    ordered = timestamps == sorted(timestamps)
    duplicates = len(timestamps) - len(unique)
    missing = sorted(expected - unique)
    extra = sorted(unique - expected)
    return {
        "rows": len(timestamps),
        "unique_timestamps": len(unique),
        "duplicates": duplicates,
        "strictly_ordered": ordered and duplicates == 0,
        "missing_timestamps_utc": [utc(value) for value in missing],
        "extra_timestamps_utc": [utc(value) for value in extra],
        "missing_count": len(missing),
        "extra_count": len(extra),
    }


def parse_ohlc_rows(rows: list[dict[str, str]], label: str) -> list[int]:
    timestamps = []
    for index, row in enumerate(rows):
        try:
            timestamp = int(row["open_time"])
            values = [float(row[key]) for key in ("open", "high", "low", "close")]
            if not all(math.isfinite(value) and value > 0 for value in values):
                raise ValueError("OHLC values must be finite and positive")
            open_price, high, low, close = values
            if high < max(open_price, low, close) or low > min(open_price, high, close):
                raise ValueError("inconsistent OHLC range")
            timestamps.append(timestamp)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{label}: invalid row {index}: {exc}") from exc
    return timestamps


def download_verified(url_root: str, filename: str, out_dir: Path) -> tuple[bytes, str]:
    archive_url = f"{url_root}/{filename}"
    checksum_url = f"{archive_url}.CHECKSUM"
    archive = download(archive_url)
    checksum = download(checksum_url).decode("utf-8", errors="replace")
    digest = verify_checksum(archive, checksum, filename)
    (out_dir / filename).write_bytes(archive)
    return archive, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="artifacts/binance-nov2023")
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    expected = set(range(START_MS, END_MS, BAR_MS))
    raw_hashes: dict[str, str] = {}
    errors: list[str] = []

    kline_root = (
        f"{BASE}/data/futures/um/monthly/klines/{SYMBOL}/{INTERVAL}"
    )
    kline_name = f"{SYMBOL}-{INTERVAL}-2023-11.zip"
    try:
        archive, digest = download_verified(kline_root, kline_name, out_dir)
        raw_hashes[kline_name] = digest
        kline_rows = parse_archive(archive, kline_name)
        kline_times = parse_ohlc_rows(kline_rows, kline_name)
        kline_audit = audit_timestamps(kline_times, expected)
    except Exception as exc:
        errors.append(f"Klines: {type(exc).__name__}: {exc}")
        kline_audit = None

    mark_times: list[int] = []
    mark_day_counts: list[dict] = []
    mark_root = (
        f"{BASE}/data/futures/um/daily/markPriceKlines/{SYMBOL}/{INTERVAL}"
    )
    for day in range(1, 31):
        date = f"2023-11-{day:02d}"
        filename = f"{SYMBOL}-{INTERVAL}-{date}.zip"
        try:
            archive, digest = download_verified(mark_root, filename, out_dir)
            raw_hashes[filename] = digest
            rows = parse_archive(archive, filename)
            timestamps = parse_ohlc_rows(rows, filename)
            mark_times.extend(timestamps)
            mark_day_counts.append({
                "date": date,
                "rows": len(rows),
                "status": "PASS" if len(rows) == 96 else "REVIEW",
            })
        except Exception as exc:
            errors.append(f"Mark Price {date}: {type(exc).__name__}: {exc}")
            mark_day_counts.append({
                "date": date,
                "rows": None,
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            })

    mark_audit = audit_timestamps(mark_times, expected) if mark_times else None
    common_count = None
    klines_without_mark = None
    mark_without_klines = None
    if kline_audit is not None and mark_audit is not None:
        kline_set = set(kline_times)
        mark_set = set(mark_times)
        common_count = len(kline_set & mark_set)
        klines_without_mark = sorted(kline_set - mark_set)
        mark_without_klines = sorted(mark_set - kline_set)

    funding_report = {
        "status": "UNVERIFIED",
        "records": None,
        "first_funding_time_utc": None,
        "last_funding_time_utc": None,
        "sha256": None,
        "note": "Funding is fetched from the public Binance USD-M fundingRate endpoint; no funding cost is applied by this preflight.",
    }
    funding_url = API + "/fapi/v1/fundingRate?" + urllib.parse.urlencode({
        "symbol": SYMBOL,
        "startTime": START_MS,
        "endTime": END_MS - 1,
        "limit": 1000,
    })
    try:
        funding_bytes = download(funding_url)
        funding_rows = json.loads(funding_bytes.decode("utf-8"))
        if not isinstance(funding_rows, list):
            raise ValueError("fundingRate response is not a JSON list")
        funding_times = sorted(int(row["fundingTime"]) for row in funding_rows)
        if any(not math.isfinite(float(row["fundingRate"])) for row in funding_rows):
            raise ValueError("fundingRate contains non-finite values")
        if len(funding_times) != len(set(funding_times)):
            raise ValueError("fundingRate contains duplicate fundingTime values")
        funding_report.update({
            "status": "PASS" if funding_rows else "REVIEW",
            "records": len(funding_rows),
            "first_funding_time_utc": utc(funding_times[0]) if funding_times else None,
            "last_funding_time_utc": utc(funding_times[-1]) if funding_times else None,
            "sha256": sha256(funding_bytes),
            "intervals_hours": [
                round((right - left) / 3_600_000, 6)
                for left, right in zip(funding_times, funding_times[1:])
            ],
        })
        (out_dir / "funding-rates.json").write_bytes(funding_bytes)
    except Exception as exc:
        funding_report["error"] = f"{type(exc).__name__}: {exc}"
        errors.append(f"Funding: {funding_report['error']}")

    raw_manifest = {
        "source_provider": "Binance Public Data / Binance USD-M Futures API",
        "source_url": BASE,
        "symbol": SYMBOL,
        "market_type": "futures",
        "timeframe": INTERVAL,
        "data_start_utc": utc(START_MS),
        "data_end_exclusive_utc": utc(END_MS),
        "raw_file_sha256": dict(sorted(raw_hashes.items())),
        "funding_api_sha256": funding_report["sha256"],
    }
    raw_manifest_bytes = json.dumps(
        raw_manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report = {
        "report_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "BINANCE_USDM_BTCUSDT_15M_NOVEMBER_2023",
        "data_window": {
            "start_utc": utc(START_MS),
            "end_exclusive_utc": utc(END_MS),
            "interval_ms": BAR_MS,
            "expected_klines": EXPECTED_BARS,
        },
        "klines": kline_audit,
        "mark_price": {
            "audit": mark_audit,
            "daily_files": mark_day_counts,
        },
        "alignment": {
            "common_timestamps": common_count,
            "klines_without_mark_price_utc": [
                utc(value) for value in (klines_without_mark or [])
            ],
            "mark_price_without_klines_utc": [
                utc(value) for value in (mark_without_klines or [])
            ],
        },
        "funding": funding_report,
        "raw_manifest_sha256": sha256(raw_manifest_bytes),
        "downloaded_archive_count": len(raw_hashes),
        "errors": errors,
        "training_gate": {
            "status": "BLOCKED_NOT_READY",
            "training_started": False,
            "runtime_eligible": False,
            "reasons": [
                "This workflow is a data-integrity preflight, not a training run.",
                "The current Advisor approval implementation is scoped to external-wallet position outcomes; Binance market-event outcomes need a separate, explicitly named approval scope and provenance contract.",
                "The current Futures backtest does not yet consume the historical funding-rate schedule and synchronized mark-price OHLC for liquidation checks; do not create net-R labels from a constant placeholder funding rate.",
                "The existing approval policy requires at least 200 fully matured training labels per fold, 3 valid chronological folds, and at least 100 OOS signals/outcomes. A single month and one symbol may not satisfy these gates; measure actual eligible trade counts before training.",
                "The known missing mark-price candle must remain missing; any sample whose execution/liquidation path depends on it must be excluded and counted.",
            ],
        },
    }
    report_path = out_dir / "data-quality-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"REPORT {report_path}")
    print(f"KLINES_ROWS {kline_audit['rows'] if kline_audit else 'UNAVAILABLE'}")
    print(f"MARK_PRICE_ROWS {mark_audit['rows'] if mark_audit else 'UNAVAILABLE'}")
    print(f"COMMON_TIMESTAMPS {common_count}")
    print(f"FUNDING_STATUS {funding_report['status']}")
    print("TRAINING_GATE BLOCKED_NOT_READY")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
