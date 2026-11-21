from __future__ import annotations

import io
import zipfile

import pytest

from scripts.binance_nov2023_data_preflight import (
    BAR_MS,
    EXPECTED_MARK_MISSING_MS,
    START_MS,
    END_MS,
    audit_timestamps,
    parse_archive,
    verify_checksum,
)


def test_timestamp_audit_identifies_the_known_single_mark_price_gap():
    expected = set(range(START_MS, END_MS, BAR_MS))
    observed = sorted(expected - {EXPECTED_MARK_MISSING_MS})

    result = audit_timestamps(observed, expected)

    assert result["rows"] == 2879
    assert result["unique_timestamps"] == 2879
    assert result["duplicates"] == 0
    assert result["missing_count"] == 1
    assert result["missing_timestamps_utc"] == [
        "2023-11-10T03:45:00+00:00"
    ]
    assert result["extra_count"] == 0
    assert result["strictly_ordered"] is True


def test_timestamp_audit_reports_duplicates_and_out_of_window_rows():
    expected = {0, BAR_MS, 2 * BAR_MS}
    observed = [0, BAR_MS, BAR_MS, 4 * BAR_MS]

    result = audit_timestamps(observed, expected)

    assert result["duplicates"] == 1
    assert result["missing_count"] == 1
    assert result["extra_count"] == 1
    assert result["strictly_ordered"] is False


def test_checksum_verification_requires_exact_sha256():
    payload = b"binance archive"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    assert verify_checksum(payload, f"{digest}  sample.zip", "sample.zip") == digest

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_checksum(payload, f"{'0' * 64}  sample.zip", "sample.zip")


def test_archive_parser_requires_one_csv_with_open_time_header():
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w") as archive:
        archive.writestr("sample.csv", "open_time,open,high,low,close\n1,1,2,0.5,1.5\n")

    rows = parse_archive(memory.getvalue(), "sample.zip")
    assert rows == [{
        "open_time": "1",
        "open": "1",
        "high": "2",
        "low": "0.5",
        "close": "1.5",
    }]
