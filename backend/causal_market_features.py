"""Causal Wilder ATR and Mark Price liquidation guards for Vike V3.1 research."""
from __future__ import annotations
import math
from typing import Iterable, Literal

BAR_INTERVAL_MS = 15 * 60 * 1000
BAR_CLOSE_OFFSET_MS = BAR_INTERVAL_MS - 1

def _validate_ohlc(rows: Iterable, *, stream: str) -> list:
    values = list(rows)
    if not values:
        raise ValueError(f"{stream} bars are required")
    previous = None
    for i, bar in enumerate(values):
        if (isinstance(bar.open_time_ms, bool) or not isinstance(bar.open_time_ms, int)
            or isinstance(bar.close_time_ms, bool) or not isinstance(bar.close_time_ms, int)):
            raise ValueError(f"{stream}[{i}] timestamps must be integer milliseconds")
        if bar.open_time_ms % BAR_INTERVAL_MS:
            raise ValueError(f"{stream}[{i}] open timestamp is not on a UTC 15-minute boundary")
        if bar.close_time_ms != bar.open_time_ms + BAR_CLOSE_OFFSET_MS:
            raise ValueError(f"{stream}[{i}] is not a complete 15-minute candle")
        if previous is not None and bar.open_time_ms <= previous:
            raise ValueError(f"{stream} timestamps must be strictly increasing and unique")
        previous = bar.open_time_ms
        raw = (bar.open, bar.high, bar.low, bar.close)
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in raw):
            raise ValueError(f"{stream}[{i}] OHLC must be numeric")
        o, h, l, c = map(float, raw)
        if not all(map(math.isfinite, (o, h, l, c))) or min(o, h, l, c) <= 0:
            raise ValueError(f"{stream}[{i}] OHLC must be finite and positive")
        if h < max(o, l, c) or l > min(o, h, c):
            raise ValueError(f"{stream}[{i}] has inconsistent OHLC")
    return values

def wilder_atr(bars: Iterable, period: int = 14) -> list[float | None]:
    """Causal Wilder ATR; first TR=H-L, SMA seed, recursive Wilder smoothing."""
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive integer")
    rows = _validate_ohlc(bars, stream="trade")
    output: list[float | None] = [None] * len(rows)
    previous_close = None
    previous_atr = None
    true_ranges = []
    for i, bar in enumerate(rows):
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        tr = high-low if previous_close is None else max(
            high-low, abs(high-previous_close), abs(low-previous_close))
        true_ranges.append(tr)
        if i == period-1:
            previous_atr = sum(true_ranges) / period
            output[i] = previous_atr
        elif i >= period:
            previous_atr = (previous_atr * (period-1) + tr) / period
            output[i] = previous_atr
        previous_close = close
    return output

def validate_mark_price_alignment(trade_bars: Iterable, mark_bars: Iterable) -> dict:
    trade = _validate_ohlc(trade_bars, stream="trade")
    mark = _validate_ohlc(mark_bars, stream="mark_price")
    trade_times = [b.open_time_ms for b in trade]
    mark_times = [b.open_time_ms for b in mark]
    if trade_times != mark_times:
        missing = sorted(set(trade_times)-set(mark_times))
        extra = sorted(set(mark_times)-set(trade_times))
        raise ValueError(f"Mark Price coverage mismatch: missing={len(missing)}, extra={len(extra)}")
    return {"status":"PASS", "matched_bars":len(trade), "missing_bars":0, "extra_bars":0}

def mark_price_liquidation_breach(*, side: Literal["LONG","SHORT"],
                                  mark_bars: Iterable, liquidation_price: float) -> dict:
    """Check Mark Price ranges against an exchange-computed liquidation price."""
    if side not in ("LONG","SHORT"):
        raise ValueError("side must be LONG or SHORT")
    if (isinstance(liquidation_price,bool) or not isinstance(liquidation_price,(int,float))
        or not math.isfinite(float(liquidation_price)) or liquidation_price <= 0):
        raise ValueError("liquidation_price must be finite and positive")
    rows = _validate_ohlc(mark_bars, stream="mark_price")
    price = float(liquidation_price)
    for bar in rows:
        breached = float(bar.low) <= price if side=="LONG" else float(bar.high) >= price
        if breached:
            return {"status":"BREACH","breach_time_ms":bar.open_time_ms,
                    "liquidation_price":price,"side":side}
    return {"status":"CLEAR","checked_bars":len(rows),"liquidation_price":price,"side":side}
