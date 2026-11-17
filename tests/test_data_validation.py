import pytest

from backend.data_validation import validate_scan_candles, timeframe_milliseconds


def hourly_candles(count=501):
    rows = []
    for index in range(1, count + 1):
        price = 100.0 + index
        rows.append([
            index * 3_600_000, price, price + 2, price - 2, price + 1, 1000.0
        ])
    return rows


def test_scan_uses_only_closed_candles_and_keeps_500():
    rows = hourly_candles(501)
    result = validate_scan_candles(
        rows, "1h", now_ms=501 * 3_600_000, min_candles=500
    )
    assert len(result) == 500
    assert result[-1][0] == 500 * 3_600_000
    assert result[-1][0] + timeframe_milliseconds("1h") <= 501 * 3_600_000


def test_scan_rejects_insufficient_closed_candles():
    with pytest.raises(ValueError, match="DATA_INSUFFICIENT_CLOSED_CANDLES"):
        validate_scan_candles(
            hourly_candles(100), "1h", now_ms=101 * 3_600_000, min_candles=500
        )


@pytest.mark.parametrize("mutation,reason", [
    ("duplicate", "DATA_NON_CHRONOLOGICAL"),
    ("gap", "DATA_GAP"),
    ("bad_ohlc", "DATA_INVALID_OHLC"),
    ("negative_volume", "DATA_INVALID_VALUE"),
])
def test_scan_rejects_invalid_ohlcv(mutation, reason):
    rows = hourly_candles(501)
    if mutation == "duplicate":
        rows[10][0] = rows[9][0]
    elif mutation == "gap":
        rows[10][0] += 3_600_000
    elif mutation == "bad_ohlc":
        rows[10][2] = rows[10][4] - 1
    elif mutation == "negative_volume":
        rows[10][5] = -1
    with pytest.raises(ValueError, match=reason):
        validate_scan_candles(
            rows, "1h", now_ms=502 * 3_600_000, min_candles=500
        )


@pytest.mark.parametrize("timeframe,expected", [
    ("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000),
    ("1d", 86_400_000),
])
def test_timeframe_milliseconds(timeframe, expected):
    assert timeframe_milliseconds(timeframe) == expected
