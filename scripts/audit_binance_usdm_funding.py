#!/usr/bin/env python3
"""Offline structural audit for Binance USD-M funding-rate CSV files.

This audit checks schema, numeric validity, ordering, duplicates, requested
window bounds, and cadence consistency. It deliberately never declares
historical coverage verified: cadence and boundary checks cannot prove that
the exchange returned every expected event.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_HEADERS = ["funding_time", "funding_rate", "mark_price"]
DEFAULT_CADENCE_MS = 8 * 60 * 60 * 1000


def parse_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Dates must include UTC offset or Z")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def audit_funding_csv(
    path: Path,
    start_ms: int,
    end_ms: int,
    cadence_ms: int = DEFAULT_CADENCE_MS,
    tolerance_ms: int = 60_000,
) -> dict:
    if end_ms <= start_ms:
        raise ValueError("end_ms must be greater than start_ms")
    if cadence_ms <= 0 or tolerance_ms < 0:
        raise ValueError("cadence_ms must be positive and tolerance_ms nonnegative")

    timestamps: list[int] = []
    invalid_rows: list[dict] = []
    out_of_window: list[int] = []
    in_window_timestamps: list[int] = []
    invalid_numeric = 0

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED_HEADERS:
            raise ValueError(
                f"Unexpected funding CSV schema: {reader.fieldnames!r}; "
                f"expected {EXPECTED_HEADERS!r}"
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                timestamp = int(row["funding_time"])
                rate = float(row["funding_rate"])
                mark_price = float(row["mark_price"])
                if not math.isfinite(rate) or not math.isfinite(mark_price) or mark_price <= 0:
                    raise ValueError("non-finite rate/price or non-positive mark price")
            except (TypeError, ValueError, OverflowError) as exc:
                invalid_numeric += 1
                invalid_rows.append({"line": line_number, "reason": str(exc)})
                continue
            timestamps.append(timestamp)
            if timestamp < start_ms or timestamp >= end_ms:
                out_of_window.append(timestamp)
            else:
                in_window_timestamps.append(timestamp)

    counts = Counter(timestamps)
    duplicates = sorted(ts for ts, count in counts.items() if count > 1)
    ordered = all(a < b for a, b in zip(timestamps, timestamps[1:]))
    # Cadence is evaluated only inside the requested half-open window.
    # Out-of-window rows remain a structural failure, but must not create
    # artificial cadence deviations at the window boundary.
    unique_sorted = sorted(set(in_window_timestamps))
    intervals = [b - a for a, b in zip(unique_sorted, unique_sorted[1:])]
    cadence_deviations = [
        {"from_ms": a, "to_ms": b, "interval_ms": b - a}
        for a, b in zip(unique_sorted, unique_sorted[1:])
        if abs((b - a) - cadence_ms) > tolerance_ms
    ]

    first = timestamps[0] if timestamps else None
    last = timestamps[-1] if timestamps else None
    return {
        "file": str(path),
        "rows": len(timestamps) + invalid_numeric,
        "valid_rows": len(timestamps),
        "invalid_numeric_rows": invalid_numeric,
        "invalid_rows": invalid_rows,
        "duplicates": duplicates,
        "strictly_increasing": ordered,
        "out_of_window_timestamps": out_of_window,
        "first_timestamp_ms": first,
        "first_timestamp_utc": (
            datetime.fromtimestamp(first / 1000, tz=timezone.utc).isoformat()
            if first is not None else None
        ),
        "last_timestamp_ms": last,
        "last_timestamp_utc": (
            datetime.fromtimestamp(last / 1000, tz=timezone.utc).isoformat()
            if last is not None else None
        ),
        "interval_count": len(intervals),
        "cadence_ms": cadence_ms,
        "cadence_tolerance_ms": tolerance_ms,
        "cadence_deviations": cadence_deviations,
        "cadence_consistent": bool(intervals) and not cadence_deviations,
        "structural_status": (
            "PASS" if timestamps and not invalid_numeric and not duplicates
            and ordered and not out_of_window and not cadence_deviations else "FAIL"
        ),
        "coverage_status": "NOT_VERIFIED",
        "coverage_note": (
            "A structurally consistent cadence and observed boundary events do "
            "not independently prove that every historical funding event was returned."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--start", required=True, help="Inclusive ISO-8601 timestamp")
    parser.add_argument("--end", required=True, help="Exclusive ISO-8601 timestamp")
    parser.add_argument("--cadence-hours", type=float, default=8.0)
    parser.add_argument("--tolerance-seconds", type=float, default=60.0)
    args = parser.parse_args()
    if args.cadence_hours <= 0 or args.tolerance_seconds < 0:
        parser.error("cadence must be positive and tolerance nonnegative")
    result = audit_funding_csv(
        args.csv, parse_ms(args.start), parse_ms(args.end),
        int(args.cadence_hours * 60 * 60 * 1000),
        int(args.tolerance_seconds * 1000),
    )
    print(json.dumps(result, indent=2))
    return 0 if result["structural_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
