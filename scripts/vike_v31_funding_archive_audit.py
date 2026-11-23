#!/usr/bin/env python3
"""Build a read-only Funding archive review packet; never self-certifies completeness."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.vike_exploratory_train import MONTHS, SYMBOLS, month_bounds, resolve_csv, sha256_file


def audit_funding_csv(path: Path, start_ms: int, end_ms: int) -> dict:
    rows = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"funding_time", "funding_rate", "mark_price"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path}: Funding schema missing required columns")
        for line, row in enumerate(reader, 2):
            try:
                timestamp = int(row["funding_time"])
                rate = float(row["funding_rate"])
                mark = float(row["mark_price"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{path}:{line}: malformed Funding row") from exc
            if (not start_ms <= timestamp < end_ms or not math.isfinite(rate)
                    or not math.isfinite(mark) or mark <= 0):
                raise ValueError(f"{path}:{line}: invalid/out-of-window Funding row")
            rows.append((timestamp, rate, mark))
    if not rows:
        raise ValueError(f"{path}: Funding file contains no records")
    timestamps = [row[0] for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError(f"{path}: duplicate Funding timestamps")
    if timestamps != sorted(timestamps):
        raise ValueError(f"{path}: Funding timestamps are not ascending")
    intervals = Counter(
        round((timestamps[i] - timestamps[i - 1]) / 3_600_000, 6)
        for i in range(1, len(timestamps))
    )
    return {
        "structural_status": "PASS",
        "row_count": len(rows),
        "first_funding_time_ms": timestamps[0],
        "last_funding_time_ms": timestamps[-1],
        "funding_interval_hours_counts": dict(sorted(intervals.items())),
        "funding_rate_min": min(row[1] for row in rows),
        "funding_rate_max": max(row[1] for row in rows),
        "mark_price_min": min(row[2] for row in rows),
        "mark_price_max": max(row[2] for row in rows),
    }


def build_review_packet(drive_root: Path, symbols: list[str]) -> dict:
    drive_root = drive_root.expanduser().resolve()
    raw_root = (drive_root / "raw-staging").resolve()
    output_root = (drive_root / "derived" / "funding-audit").resolve()
    if not raw_root.is_dir():
        raise FileNotFoundError(f"raw-staging directory not found: {raw_root}")
    if output_root == raw_root or raw_root in output_root.parents:
        raise ValueError("Funding audit output must remain outside raw-staging")
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in SYMBOLS for s in symbols):
        raise ValueError("symbols must be a unique subset of the frozen symbol universe")

    entries = []
    for symbol in symbols:
        for month in MONTHS:
            start_ms, end_ms = month_bounds(month)
            path = resolve_csv(raw_root, month, symbol, "funding")
            audit = audit_funding_csv(path, start_ms, end_ms)
            entries.append({
                "symbol": symbol, "month": month,
                "path_relative_to_drive": str(path.relative_to(drive_root)),
                "funding_file_sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                **audit,
            })

    output_root.mkdir(parents=True, exist_ok=True)
    evidence_path = output_root / "funding-archive-evidence.json"
    evidence = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "venue": "Binance USD-M",
        "market_type": "USDT perpetual futures",
        "development_months": list(MONTHS),
        "symbols": symbols,
        "file_count": len(entries),
        "structural_pass_count": sum(row["structural_status"] == "PASS" for row in entries),
        "coverage_claim": "NOT_ESTABLISHED_BY_STRUCTURAL_AUDIT",
        "records": entries,
        "review_notes": [
            "This packet proves file hashes and basic CSV structure only.",
            "It does not prove that every exchange funding settlement is present.",
            "Review event completeness against an authoritative Binance archive/API retrieval log.",
            "Funding intervals vary by contract and may change; do not impose a universal 8-hour cadence.",
            "Do not change PENDING_REVIEW to VERIFIED without independent semantic and completeness review.",
        ],
    }
    evidence_path.write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    certificate = {
        "schema_version": 1,
        "coverage_status": "PENDING_REVIEW",
        "evidence_artifact_path": evidence_path.name,
        "evidence_sha256": sha256_file(evidence_path),
        "reviewed_by": None,
        "reviewed_at_utc": None,
        "semantic_basis": None,
        "symbol_months": [{
            "symbol": row["symbol"], "month": row["month"],
            "status": "PENDING_REVIEW",
            "funding_file_sha256": row["funding_file_sha256"],
        } for row in entries],
        "warning": "This is a review template, not a certificate accepted by the labeling gate.",
    }
    certificate_path = output_root / "funding-coverage-certificate.template.json"
    certificate_path.write_text(json.dumps(certificate, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return {
        "output_dir": str(output_root),
        "evidence_path": str(evidence_path),
        "certificate_template_path": str(certificate_path),
        "file_count": len(entries),
        "structural_pass_count": evidence["structural_pass_count"],
        "coverage_status": "PENDING_REVIEW",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-root", required=True, type=Path)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    args = parser.parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    try:
        print(json.dumps(build_review_packet(args.drive_root, symbols), indent=2))
        return 0
    except Exception as exc:
        parser.exit(2, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
