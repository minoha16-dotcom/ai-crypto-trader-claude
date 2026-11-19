from datetime import datetime, timezone

import pytest

from backend.historical_data_validation import (
    inspect_timestamp_series,
    normalize_epoch_ms,
)


NOW = int(datetime(2026, 11, 19, tzinfo=timezone.utc).timestamp() * 1000)


def test_normalizes_milliseconds():
    value = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert normalize_epoch_ms(value, now_ms=NOW) == value


def test_normalizes_microseconds_without_precision_drift():
    value_ms = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert normalize_epoch_ms(value_ms * 1000 + 999, now_ms=NOW) == value_ms


@pytest.mark.parametrize("value", [None, True, 1_700_000_000, -1, float("nan"), float("inf")])
def test_rejects_invalid_or_seconds_timestamps(value):
    with pytest.raises(ValueError):
        normalize_epoch_ms(value, now_ms=NOW)


def test_rejects_future_and_implausibly_old_timestamps():
    with pytest.raises(ValueError):
        normalize_epoch_ms(NOW + 2 * 86_400_000, now_ms=NOW)
    with pytest.raises(ValueError):
        normalize_epoch_ms(100_000_000_000, now_ms=NOW)


def test_series_reports_gaps_without_filling():
    start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    result = inspect_timestamp_series(
        [start, start + 15 * 60_000, start + 45 * 60_000],
        expected_interval_ms=15 * 60_000,
        now_ms=NOW,
    )
    assert result["strictly_increasing"] is True
    assert result["gap_count"] == 1
    assert result["gaps"][0]["missing_intervals"] == 1


def test_series_detects_duplicate_and_out_of_order_rows():
    start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    result = inspect_timestamp_series(
        [start, start, start - 60_000], now_ms=NOW
    )
    assert result["duplicate_adjacent_count"] == 1
    assert result["out_of_order_adjacent_count"] == 1
    assert result["strictly_increasing"] is False
