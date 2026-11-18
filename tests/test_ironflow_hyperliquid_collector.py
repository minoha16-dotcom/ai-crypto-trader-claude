from datetime import datetime, timezone
import pytest
from scripts.download_ironflow_hyperliquid_candles import aggregate, month_range, validate_hourly

def bar(ts, o=100.0, h=102.0, l=99.0, c=101.0, v=5.0):
    return [ts, o, h, l, c, v]

def test_month_range_crosses_year_boundary():
    assert list(month_range("2025-11", "2026-02")) == ["2025-11", "2025-12", "2026-01", "2026-02"]

def test_aggregate_only_emits_complete_utc_buckets():
    base = int(datetime(2025, 5, 26, tzinfo=timezone.utc).timestamp() * 1000)
    hourly = [bar(base+i*3600000, o=100+i, h=105+i, l=95+i, c=102+i, v=2) for i in range(6)]
    result = aggregate(hourly, 4)
    assert len(result) == 1
    assert result[0] == [base, 100, 108, 95, 105, 8]

def test_aggregate_does_not_bridge_missing_hour():
    base = int(datetime(2025, 5, 26, tzinfo=timezone.utc).timestamp() * 1000)
    hourly = [bar(base+i*3600000) for i in (0, 1, 3, 4)]
    assert aggregate(hourly, 4) == []

def test_validate_hourly_reports_gaps_and_deduplicates_identical_rows():
    base = int(datetime(2025, 5, 26, tzinfo=timezone.utc).timestamp() * 1000)
    rows, gaps = validate_hourly("BTC", [bar(base), bar(base), bar(base+2*3600000)])
    assert len(rows) == 2
    assert gaps[0]["missing_hours"] == 1

def test_validate_hourly_rejects_conflicting_duplicates():
    base = int(datetime(2025, 5, 26, tzinfo=timezone.utc).timestamp() * 1000)
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        validate_hourly("BTC", [bar(base), bar(base, c=999)])
