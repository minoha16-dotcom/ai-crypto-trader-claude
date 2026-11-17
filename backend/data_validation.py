"""Strict OHLCV validation for live technical scans.

Exchange timestamps are CCXT milliseconds and identify candle open times.
Only candles whose full timeframe has elapsed are eligible for analysis.
"""
import math
import re
import time


MIN_SCAN_CANDLES = 500


def timeframe_milliseconds(timeframe):
    match = re.fullmatch(r"(\d+)([mhdw])", str(timeframe).strip().lower())
    if not match:
        raise ValueError(f"Unsupported scan timeframe: {timeframe}")
    amount = int(match.group(1))
    if amount < 1:
        raise ValueError(f"Unsupported scan timeframe: {timeframe}")
    unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    return amount * unit[match.group(2)]


def validate_scan_candles(candles, timeframe, *, now_ms=None, min_candles=MIN_SCAN_CANDLES):
    """Return the most recent closed candles or raise ValueError with a reason.

    The full response is validated before the forming candle is removed so
    duplicate, malformed, or out-of-order rows cannot be hidden at the tail.
    Gaps are rejected rather than silently interpolated.
    """
    if not isinstance(candles, (list, tuple)):
        raise ValueError("DATA_INVALID_SHAPE: OHLCV response must be a list")
    interval = timeframe_milliseconds(timeframe)
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms):
        raise ValueError("DATA_INVALID_CLOCK: current timestamp is invalid")

    previous = None
    normalized = []
    for index, candle in enumerate(candles):
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            raise ValueError(f"DATA_INVALID_SHAPE: candle {index} must contain OHLCV")
        try:
            timestamp, open_price, high, low, close, volume = map(float, candle[:6])
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"DATA_INVALID_NUMBER: candle {index} contains non-numeric values")
        if not all(math.isfinite(value) for value in (
            timestamp, open_price, high, low, close, volume
        )):
            raise ValueError(f"DATA_NON_FINITE: candle {index} contains NaN or infinity")
        if timestamp <= 0 or min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"DATA_INVALID_VALUE: candle {index} has non-positive price or negative volume")
        if high < max(open_price, close, low) or low > min(open_price, close):
            raise ValueError(f"DATA_INVALID_OHLC: candle {index} has inconsistent high/low")
        if not math.isclose(timestamp, round(timestamp), rel_tol=0, abs_tol=0.001):
            raise ValueError(f"DATA_INVALID_TIMESTAMP: candle {index} timestamp is not integral")
        timestamp = int(timestamp)
        if timestamp % interval != 0:
            raise ValueError(f"DATA_MISALIGNED_TIMESTAMP: candle {index} is not aligned to {timeframe}")
        if previous is not None:
            delta = timestamp - previous
            if delta <= 0:
                raise ValueError("DATA_NON_CHRONOLOGICAL: timestamps must be strictly increasing and unique")
            if delta != interval:
                raise ValueError(
                    f"DATA_GAP: expected {interval}ms between candles, received {delta}ms"
                )
        previous = timestamp
        normalized.append([timestamp, open_price, high, low, close, volume])

    closed = [row for row in normalized if row[0] + interval <= now_ms]
    if len(closed) < min_candles:
        raise ValueError(
            f"DATA_INSUFFICIENT_CLOSED_CANDLES: need {min_candles}, received {len(closed)}"
        )
    return closed[-min_candles:]
