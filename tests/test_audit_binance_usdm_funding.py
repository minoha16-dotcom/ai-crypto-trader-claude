from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.audit_binance_usdm_funding import audit_funding_csv


def write_funding(path: Path, rows: list[tuple]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time", "funding_rate", "mark_price"])
        writer.writerows(rows)


def test_valid_monthly_cadence_passes_structure_but_not_coverage(tmp_path: Path):
    path = tmp_path / "funding.csv"
    start = 1_735_689_600_000
    cadence = 8 * 60 * 60 * 1000
    rows = [(start + 15 + i * cadence, "0.0001", "100000") for i in range(93)]
    write_funding(path, rows)

    report = audit_funding_csv(path, start, start + 31 * 24 * 60 * 60 * 1000)

    assert report["structural_status"] == "PASS"
    assert report["valid_rows"] == 93
    assert report["duplicates"] == []
    assert report["cadence_consistent"] is True
    assert report["coverage_status"] == "NOT_VERIFIED"


def test_duplicate_and_non_monotonic_rows_fail(tmp_path: Path):
    path = tmp_path / "funding.csv"
    write_funding(path, [(1000, "0.1", "100"), (1000, "0.1", "100"), (900, "0.1", "100")])

    report = audit_funding_csv(path, 0, 2000, cadence_ms=100, tolerance_ms=0)

    assert report["structural_status"] == "FAIL"
    assert report["duplicates"] == [1000]
    assert report["strictly_increasing"] is False


def test_invalid_numeric_and_nonpositive_mark_price_fail(tmp_path: Path):
    path = tmp_path / "funding.csv"
    write_funding(path, [(1000, "nan", "100"), (2000, "0.1", "0")])

    report = audit_funding_csv(path, 0, 3000, cadence_ms=1000, tolerance_ms=0)

    assert report["invalid_numeric_rows"] == 2
    assert report["structural_status"] == "FAIL"


def test_out_of_window_and_cadence_gap_fail(tmp_path: Path):
    path = tmp_path / "funding.csv"
    write_funding(path, [(900, "0.1", "100"), (1000, "0.1", "100"), (3000, "0.1", "100")])

    report = audit_funding_csv(path, 1000, 4000, cadence_ms=1000, tolerance_ms=0)

    assert report["out_of_window_timestamps"] == [900]
    assert len(report["cadence_deviations"]) == 1
    assert report["structural_status"] == "FAIL"


def test_wrong_schema_is_rejected(tmp_path: Path):
    path = tmp_path / "funding.csv"
    path.write_text("time,rate,price\n1000,0.1,100\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unexpected funding CSV schema"):
        audit_funding_csv(path, 0, 2000)
