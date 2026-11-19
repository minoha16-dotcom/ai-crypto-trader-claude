"""Strict timestamp and historical-series validation helpers.

Binance historical endpoints may return millisecond timestamps while some
archives use microseconds. Normalize only recognizable epoch magnitudes and
reject seconds/ambiguous values rather than silently guessing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Iterable


MIN_SUPPORTED_MS = int(datetime(2009, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def normalize_epoch_ms(value, *, now_ms=None, field="timestamp") -> int:
    """Normalize a Unix epoch in ms or µs to UTC milliseconds.

    Seconds are deliberately rejected. The plausible-date guard catches unit
    mistakes and malformed values before they can contaminate coverage reports.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field}: boolean is not a timestamp")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field}: timestamp is not numeric") from None
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError(f"{field}: timestamp must be a finite positive integer")

    raw = int(number)
    if 100_000_000_000 <= raw < 100_000_000_000_000:
        timestamp_ms = raw
        unit = "ms"
    elif 100_000_000_000_000 <= raw < 100_000_000_000_000_000:
        timestamp_ms = raw // 1000
        unit = "us"
    else:
        raise ValueError(
            f"{field}: unsupported epoch magnitude; expected milliseconds or microseconds"
        )

    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if timestamp_ms < MIN_SUPPORTED_MS or timestamp_ms > int(now_ms) + 86_400_000:
        raise ValueError(f"{field}: {unit} timestamp is outside the plausible date range")
    return timestamp_ms


def inspect_timestamp_series(
    values: Iterable,
    *,
    expected_interval_ms: int | None = None,
    now_ms=None,
    field="timestamp",
) -> dict:
    """Return chronology, duplicate and gap diagnostics without filling data."""
    normalized = [
        normalize_epoch_ms(value, now_ms=now_ms, field=f"{field}[{index}]")
        for index, value in enumerate(values)
    ]
    seen = set()
    duplicates = 0
    for timestamp in normalized:
        if timestamp in seen:
            duplicates += 1
        seen.add(timestamp)
    out_of_order = sum(
        1 for index in range(1, len(normalized))
        if normalized[index] < normalized[index - 1]
    )
    gaps = []
    if expected_interval_ms is not None:
        if isinstance(expected_interval_ms, bool) or int(expected_interval_ms) <= 0:
            raise ValueError("expected_interval_ms must be a positive integer")
        for index in range(1, len(normalized)):
            delta = normalized[index] - normalized[index - 1]
            if delta > expected_interval_ms:
                gaps.append({
                    "after_index": index - 1,
                    "before_index": index,
                    "missing_intervals": max(0, delta // expected_interval_ms - 1),
                    "delta_ms": delta,
                    "unexpected_interval": delta % expected_interval_ms != 0,
                })
            elif delta < expected_interval_ms:
                gaps.append({
                    "after_index": index - 1,
                    "before_index": index,
                    "missing_intervals": 0,
                    "delta_ms": delta,
                    "unexpected_interval": True,
                })
    return {
        "count": len(normalized),
        "first_ms": normalized[0] if normalized else None,
        "last_ms": normalized[-1] if normalized else None,
        "strictly_increasing": out_of_order == 0 and duplicates == 0,
        "duplicate_count": duplicates,
        "out_of_order_adjacent_count": out_of_order,
        "gap_count": len(gaps),
        "gaps": gaps,
    }
