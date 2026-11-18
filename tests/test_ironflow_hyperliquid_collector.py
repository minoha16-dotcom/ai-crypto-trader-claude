from datetime import datetime, timezone
import json

import pytest

from scripts.download_ironflow_hyperliquid_candles import (
    START_MONTH, END_MONTH, aggregate, month_range, validate_hourly, write_outputs,
)


def bar(ts, o=100.0, h=102.0, l=99.0, c=101.0, v=5.0):
    return [ts, o, h, l, c, v]


def utc_ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_month_range_crosses_year_boundary():
    assert list(month_range("2025-11", "2026-02")) == [
        "2025-11", "2025-12", "2026-01", "2026-02"
    ]


@pytest.mark.parametrize("value", ["2025-00", "2025-13", "25-05", "2025-5", "bad"])
def test_month_range_rejects_invalid_month(value):
    with pytest.raises(ValueError):
        list(month_range(value, "2026-01"))


def test_aggregate_only_emits_complete_utc_buckets():
    base = utc_ms("2025-05-26T00:00:00")
    hourly = [
        bar(base + i * 3600000, o=100 + i, h=105 + i, l=95 + i, c=102 + i, v=2)
        for i in range(6)
    ]
    result = aggregate(hourly, 4)
    assert len(result) == 1
    assert result[0] == [base, 100, 108, 95, 105, 8]


def test_aggregate_does_not_bridge_missing_hour():
    base = utc_ms("2025-05-26T00:00:00")
    hourly = [bar(base + i * 3600000) for i in (0, 1, 3, 4)]
    assert aggregate(hourly, 4) == []


def test_validate_hourly_reports_gaps_and_deduplicates_identical_rows():
    base = utc_ms("2025-05-26T00:00:00")
    rows, gaps = validate_hourly("BTC", [bar(base), bar(base), bar(base + 2 * 3600000)])
    assert len(rows) == 2
    assert gaps[0]["missing_hours"] == 1


def test_validate_hourly_rejects_conflicting_duplicates():
    base = utc_ms("2025-05-26T00:00:00")
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        validate_hourly("BTC", [bar(base), bar(base, c=999)])


def test_validate_hourly_rejects_non_hour_aligned_timestamp():
    base = utc_ms("2025-05-26T00:00:00")
    with pytest.raises(ValueError, match="UTC-hour-aligned"):
        validate_hourly("BTC", [bar(base + 60_000)])


def test_coverage_report_rejects_contiguous_but_short_requested_window(tmp_path):
    start = utc_ms("2025-05-25T00:00:00")
    end = utc_ms("2025-05-25T05:00:00")
    rows = [bar(start + i * 3600000) for i in range(1, 5)]
    report = write_outputs(["BTC"], {"BTC": rows}, tmp_path, [], start, end)
    group = report["groups"]["BTC"]
    assert group["status"] == "INCOMPLETE"
    assert "REQUESTED_WINDOW_NOT_FULLY_COVERED" in group["rejection_reasons"]
    assert report["overall_status"] == "REJECTED_OR_INCOMPLETE"
    persisted = json.loads((tmp_path / "coverage-report.json").read_text())
    assert persisted["groups"]["BTC"]["boundary_gaps"]


def test_coverage_report_accepts_exact_contiguous_requested_window(tmp_path):
    start = utc_ms("2025-05-25T00:00:00")
    end = utc_ms("2025-05-25T05:00:00")
    rows = [bar(start + i * 3600000) for i in range(5)]
    report = write_outputs(["BTC"], {"BTC": rows}, tmp_path, [], start, end)
    assert report["groups"]["BTC"]["status"] == "STRUCTURALLY_CONTIGUOUS"
    assert report["overall_status"] == "READY_FOR_RESEARCH_VALIDATION"


def test_research_window_starts_at_first_available_hyperliquid_hour():
    assert START_MONTH == "2025-05"
    assert END_MONTH == "2026-10"
    assert utc_ms("2025-05-25T14:00:00") == 1748181600000
