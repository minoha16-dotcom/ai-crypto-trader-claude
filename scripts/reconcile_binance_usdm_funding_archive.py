#!/usr/bin/env python3
"""Reconcile Binance USD-M REST funding CSV against Binance Vision monthly ZIP.

The archive and REST field correspondence is treated as a reconciliation
hypothesis, not an authoritative semantic guarantee. Even a perfect match
keeps coverage_status=NOT_VERIFIED until Binance documents timestamp/rate
semantics and archive completeness/finality.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

API_HEADERS = ["funding_time", "funding_rate", "mark_price"]
ARCHIVE_HEADERS = ["calc_time", "funding_interval_hours", "last_funding_rate"]


def parse_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Dates must include UTC offset or Z")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_checksum(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig").strip()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not match:
        raise ValueError("Checksum file does not contain a SHA-256 digest")
    return match.group(1).lower()


def _read_api(path: Path, start_ms: int, end_ms: int) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    invalid: list[dict] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != API_HEADERS:
            raise ValueError(f"Unexpected REST CSV schema: {reader.fieldnames!r}")
        for line, row in enumerate(reader, start=2):
            try:
                ts = int(row["funding_time"])
                rate = float(row["funding_rate"])
                mark = float(row["mark_price"])
                if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                    raise ValueError("non-finite rate/mark price or non-positive mark price")
                if not start_ms <= ts < end_ms:
                    raise ValueError("timestamp outside requested [start, end) window")
                rows.append({"timestamp_ms": ts, "rate": rate, "mark_price": mark})
            except (TypeError, ValueError, OverflowError) as exc:
                invalid.append({"line": line, "reason": str(exc)})
    return rows, invalid


def _read_archive(path: Path, start_ms: int, end_ms: int) -> tuple[list[dict], list[dict], str]:
    invalid: list[dict] = []
    with zipfile.ZipFile(path) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"Expected exactly one CSV in archive ZIP; found {csv_names!r}")
        raw = archive.read(csv_names[0]).decode("utf-8-sig")
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames != ARCHIVE_HEADERS:
        raise ValueError(
            f"Unexpected Binance Vision funding schema: {reader.fieldnames!r}; "
            f"expected {ARCHIVE_HEADERS!r}"
        )
    for line, row in enumerate(reader, start=2):
        try:
            ts = int(row["calc_time"])
            interval = float(row["funding_interval_hours"])
            rate = float(row["last_funding_rate"])
            if not math.isfinite(interval) or interval <= 0 or not math.isfinite(rate):
                raise ValueError("non-finite rate/interval or non-positive interval")
            if not start_ms <= ts < end_ms:
                raise ValueError("timestamp outside requested [start, end) window")
            rows.append({"timestamp_ms": ts, "rate": rate, "interval_hours": interval})
        except (TypeError, ValueError, OverflowError) as exc:
            invalid.append({"line": line, "reason": str(exc)})
    return rows, invalid, csv_names[0]


def _ordered_unique(rows: list[dict]) -> bool:
    timestamps = [row["timestamp_ms"] for row in rows]
    return all(a < b for a, b in zip(timestamps, timestamps[1:]))


def reconcile_funding(
    api_csv: Path,
    archive_zip: Path,
    checksum_file: Path,
    start_ms: int,
    end_ms: int,
    timestamp_tolerance_ms: int = 60_000,
    rate_tolerance: float = 1e-10,
) -> dict:
    if end_ms <= start_ms:
        raise ValueError("end_ms must be greater than start_ms")
    if timestamp_tolerance_ms < 0 or rate_tolerance < 0:
        raise ValueError("Tolerances must be nonnegative")

    expected_hash = _expected_checksum(checksum_file)
    actual_hash = _sha256(archive_zip)
    checksum_ok = expected_hash == actual_hash
    api_rows, api_invalid = _read_api(api_csv, start_ms, end_ms)
    archive_rows, archive_invalid, archive_member = _read_archive(
        archive_zip, start_ms, end_ms
    )
    api_ordered = _ordered_unique(api_rows)
    archive_ordered = _ordered_unique(archive_rows)

    # One-to-one monotonic nearest-timestamp reconciliation. Each row can be
    # matched once only; no interpolation, synthetic events, or forward-fill.
    api_sorted = sorted(api_rows, key=lambda row: row["timestamp_ms"])
    archive_sorted = sorted(archive_rows, key=lambda row: row["timestamp_ms"])
    i = j = 0
    matched = []
    api_unmatched = []
    archive_unmatched = []
    rate_mismatches = []
    while i < len(api_sorted) and j < len(archive_sorted):
        a = api_sorted[i]
        b = archive_sorted[j]
        delta = a["timestamp_ms"] - b["timestamp_ms"]
        if abs(delta) <= timestamp_tolerance_ms:
            matched.append({
                "api_timestamp_ms": a["timestamp_ms"],
                "archive_calc_time_ms": b["timestamp_ms"],
                "timestamp_delta_ms": delta,
            })
            if abs(a["rate"] - b["rate"]) > rate_tolerance:
                rate_mismatches.append({
                    "api_timestamp_ms": a["timestamp_ms"],
                    "archive_calc_time_ms": b["timestamp_ms"],
                    "api_rate": a["rate"],
                    "archive_rate": b["rate"],
                    "absolute_difference": abs(a["rate"] - b["rate"]),
                })
            i += 1
            j += 1
        elif delta < 0:
            api_unmatched.append(a["timestamp_ms"])
            i += 1
        else:
            archive_unmatched.append(b["timestamp_ms"])
            j += 1
    api_unmatched.extend(row["timestamp_ms"] for row in api_sorted[i:])
    archive_unmatched.extend(row["timestamp_ms"] for row in archive_sorted[j:])

    intervals = Counter(str(row["interval_hours"]) for row in archive_rows)
    structural_ok = (
        bool(api_rows) and bool(archive_rows)
        and not api_invalid and not archive_invalid
        and api_ordered and archive_ordered
    )
    reconciliation_ok = (
        structural_ok and checksum_ok
        and not api_unmatched and not archive_unmatched and not rate_mismatches
        and len(matched) == len(api_rows) == len(archive_rows)
    )
    return {
        "api_csv": str(api_csv),
        "archive_zip": str(archive_zip),
        "archive_member": archive_member,
        "checksum_file": str(checksum_file),
        "expected_archive_sha256": expected_hash,
        "actual_archive_sha256": actual_hash,
        "archive_checksum_verified": checksum_ok,
        "api_rows": len(api_rows),
        "archive_rows": len(archive_rows),
        "api_invalid_rows": api_invalid,
        "archive_invalid_rows": archive_invalid,
        "api_strictly_increasing": api_ordered,
        "archive_strictly_increasing": archive_ordered,
        "archive_interval_hours_distribution": dict(intervals),
        "matched_rows": len(matched),
        "api_unmatched_timestamps_ms": api_unmatched,
        "archive_unmatched_timestamps_ms": archive_unmatched,
        "rate_mismatches": rate_mismatches,
        "timestamp_tolerance_ms": timestamp_tolerance_ms,
        "rate_tolerance": rate_tolerance,
        "structural_status": "PASS" if structural_ok else "FAIL",
        "archive_reconciliation_status": "MATCH" if reconciliation_ok else "MISMATCH",
        "coverage_status": "NOT_VERIFIED",
        "coverage_note": (
            "A checksum-valid archive/REST match is strong cross-source evidence, "
            "but Binance has not authoritatively documented whether archive calc_time "
            "maps exactly to REST fundingTime, last_funding_rate to fundingRate, "
            "or the archive completeness/finality policy. Do not promote to VERIFIED "
            "until those semantics are confirmed and the full requested period is reconciled."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-csv", required=True, type=Path)
    parser.add_argument("--archive-zip", required=True, type=Path)
    parser.add_argument("--checksum", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--timestamp-tolerance-ms", type=int, default=60_000)
    parser.add_argument("--rate-tolerance", type=float, default=1e-10)
    args = parser.parse_args()
    result = reconcile_funding(
        args.api_csv, args.archive_zip, args.checksum,
        parse_ms(args.start), parse_ms(args.end),
        args.timestamp_tolerance_ms, args.rate_tolerance,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["archive_reconciliation_status"] == "MATCH" else 2


if __name__ == "__main__":
    raise SystemExit(main())
