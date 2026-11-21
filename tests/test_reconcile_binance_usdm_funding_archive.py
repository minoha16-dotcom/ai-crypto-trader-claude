from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from pathlib import Path

from scripts.reconcile_binance_usdm_funding_archive import reconcile_funding


def write_inputs(tmp_path: Path, api_rows, archive_rows):
    api = tmp_path / "api.csv"
    with api.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time", "funding_rate", "mark_price"])
        writer.writerows(api_rows)

    archive = tmp_path / "archive.zip"
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(["calc_time", "funding_interval_hours", "last_funding_rate"])
    writer.writerows(archive_rows)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("BTCUSDT-fundingRate-2025-01.csv", text.getvalue())

    checksum = tmp_path / "archive.zip.CHECKSUM"
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  archive.zip\n")
    return api, archive, checksum


def run_reconcile(tmp_path, api_rows, archive_rows):
    api, archive, checksum = write_inputs(tmp_path, api_rows, archive_rows)
    return reconcile_funding(api, archive, checksum, 0, 100_000)


def test_checksum_valid_archive_matches_api_and_keeps_coverage_unverified(tmp_path):
    report = run_reconcile(
        tmp_path,
        [(1000, "0.0001", "100"), (9000, "-0.0002", "101")],
        [(1000, "8", "0.0001"), (9000, "8", "-0.0002")],
    )
    assert report["archive_checksum_verified"] is True
    assert report["matched_rows"] == 2
    assert report["api_unmatched_timestamps_ms"] == []
    assert report["archive_unmatched_timestamps_ms"] == []
    assert report["rate_mismatches"] == []
    assert report["archive_reconciliation_status"] == "MATCH"
    assert report["coverage_status"] == "NOT_VERIFIED"


def test_missing_archive_event_is_mismatch(tmp_path):
    report = run_reconcile(
        tmp_path,
        [(1000, "0.0001", "100"), (9000, "-0.0002", "101")],
        [(1000, "8", "0.0001")],
    )
    assert report["archive_reconciliation_status"] == "MISMATCH"
    assert report["api_unmatched_timestamps_ms"] == [9000]


def test_rate_difference_is_mismatch(tmp_path):
    report = run_reconcile(
        tmp_path,
        [(1000, "0.0001", "100")],
        [(1000, "8", "0.0002")],
    )
    assert len(report["rate_mismatches"]) == 1
    assert report["archive_reconciliation_status"] == "MISMATCH"


def test_timestamp_tolerance_matches_small_offset(tmp_path):
    report = run_reconcile(
        tmp_path,
        [(1015, "0.0001", "100")],
        [(1000, "8", "0.0001")],
    )
    assert report["matched_rows"] == 1
    assert report["archive_reconciliation_status"] == "MATCH"


def test_bad_archive_checksum_is_mismatch(tmp_path):
    api, archive, checksum = write_inputs(
        tmp_path, [(1000, "0.0001", "100")], [(1000, "8", "0.0001")]
    )
    checksum.write_text("0" * 64 + "  archive.zip\n")
    report = reconcile_funding(api, archive, checksum, 0, 100_000)
    assert report["archive_checksum_verified"] is False
    assert report["archive_reconciliation_status"] == "MISMATCH"
