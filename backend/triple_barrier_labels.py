"""Deterministic, research-only Triple-Barrier event labeler.

This module is deliberately independent from the legacy Advisor/wallet cohort
pipeline. It never fetches data, fills missing observations, trains models or
places orders. Callers must prove source coverage before labeling.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Literal

Side = Literal["LONG", "SHORT"]
BAR_INTERVAL_MS = 15 * 60 * 1000
BAR_CLOSE_OFFSET_MS = BAR_INTERVAL_MS - 1


@dataclass(frozen=True)
class Bar:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class FundingEvent:
    funding_time_ms: int
    funding_rate: float
    mark_price: float


@dataclass(frozen=True)
class LabelConfig:
    atr_multiple_stop: float = 1.5
    atr_multiple_target: float = 3.0
    max_holding_bars: int = 48
    taker_fee_rate: float | None = None
    adverse_slippage_rate: float | None = None

    def validate(self) -> None:
        for name in ("atr_multiple_stop", "atr_multiple_target"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.atr_multiple_target <= self.atr_multiple_stop:
            raise ValueError("target multiple must exceed stop multiple")
        if (
            isinstance(self.max_holding_bars, bool)
            or not isinstance(self.max_holding_bars, int)
            or self.max_holding_bars < 1
        ):
            raise ValueError("max_holding_bars must be a positive integer")
        for name in ("taker_fee_rate", "adverse_slippage_rate"):
            value = getattr(self, name)
            if (
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or value >= 1
            ):
                raise ValueError(f"{name} must be explicitly set to a finite rate in [0, 1)")


def _finite_positive(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a numeric finite positive value")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


def _validate_bars(bars: Iterable[Bar]) -> list[Bar]:
    rows = list(bars)
    if not rows:
        raise ValueError("bars are required")
    previous = None
    for index, bar in enumerate(rows):
        if (
            isinstance(bar.open_time_ms, bool)
            or isinstance(bar.close_time_ms, bool)
            or not isinstance(bar.open_time_ms, int)
            or not isinstance(bar.close_time_ms, int)
        ):
            raise ValueError(f"bar[{index}] timestamps must be integer milliseconds")
        if bar.open_time_ms % BAR_INTERVAL_MS != 0:
            raise ValueError(f"bar[{index}] open timestamp must align to a UTC 15-minute boundary")
        if bar.close_time_ms != bar.open_time_ms + BAR_CLOSE_OFFSET_MS:
            raise ValueError(f"bar[{index}] must be a complete 15-minute bar")
        if previous is not None and bar.open_time_ms <= previous:
            raise ValueError("bars must be strictly ordered and unique")
        previous = bar.open_time_ms
        raw_values = [bar.open, bar.high, bar.low, bar.close]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_values):
            raise ValueError(f"bar[{index}] OHLC values must be numeric")
        values = [float(value) for value in raw_values]
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"bar[{index}] contains non-finite OHLC")
        if min(values) <= 0 or bar.high < max(bar.open, bar.close, bar.low):
            raise ValueError(f"bar[{index}] has inconsistent OHLC")
        if bar.low > min(bar.open, bar.close):
            raise ValueError(f"bar[{index}] has inconsistent OHLC")
    return rows


def _validate_funding(events: Iterable[FundingEvent]) -> list[FundingEvent]:
    rows = list(events)
    for index, event in enumerate(rows):
        if isinstance(event.funding_time_ms, bool) or not isinstance(event.funding_time_ms, int):
            raise ValueError(f"funding[{index}] timestamp must be integer milliseconds")
        if (
            isinstance(event.funding_rate, bool)
            or not isinstance(event.funding_rate, (int, float))
            or not math.isfinite(float(event.funding_rate))
        ):
            raise ValueError(f"funding[{index}] rate must be numeric and finite")
        _finite_positive(event.mark_price, f"funding[{index}].mark_price")
    rows.sort(key=lambda item: item.funding_time_ms)
    timestamps = [event.funding_time_ms for event in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("funding timestamps must be unique")
    return rows


def label_event(
    *,
    event_time_ms: int,
    side: Side,
    atr: float,
    bars: Iterable[Bar],
    funding_events: Iterable[FundingEvent],
    funding_coverage_verified: bool,
    config: LabelConfig,
) -> dict:
    """Label one event; incomplete/ambiguous evidence fails closed.

    Event time is the signal candle close. Entry is the next 15-minute bar open.
    Stop gaps are filled at the bar open (then adverse slippage), not at the
    stale stop price. If both barriers touch in one bar, SL wins.

    Funding at the exact entry timestamp is ambiguous and excluded. For an
    intrabar barrier exit, any settlement in the exit bar is ambiguous. For a
    time exit at bar close, settlements earlier in that bar are applied, while
    a settlement exactly at the close timestamp is excluded as ambiguous.
    """
    config.validate()
    if not funding_coverage_verified:
        raise ValueError("funding coverage must be independently verified before labeling")
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    if isinstance(event_time_ms, bool) or not isinstance(event_time_ms, int):
        raise ValueError("event_time_ms must be integer milliseconds")
    if event_time_ms % BAR_INTERVAL_MS != BAR_CLOSE_OFFSET_MS:
        raise ValueError("event_time_ms must align to a UTC 15-minute candle close")
    atr_value = _finite_positive(atr, "atr")
    rows = _validate_bars(bars)
    funding = _validate_funding(funding_events)

    entry_index = next(
        (i for i, bar in enumerate(rows) if bar.open_time_ms > event_time_ms),
        None,
    )
    if entry_index is None:
        return {"status": "excluded", "reason": "no_next_bar"}
    entry_bar = rows[entry_index]
    if entry_bar.open_time_ms != event_time_ms + 1:
        return {"status": "excluded", "reason": "missing_immediate_next_bar"}
    slip = float(config.adverse_slippage_rate)
    fee = float(config.taker_fee_rate)
    direction = 1.0 if side == "LONG" else -1.0
    entry_fill = entry_bar.open * (1.0 + direction * slip)
    if entry_fill <= 0:
        return {"status": "excluded", "reason": "invalid_entry_fill"}
    stop_distance = config.atr_multiple_stop * atr_value
    stop = entry_fill - direction * stop_distance
    target = entry_fill + direction * config.atr_multiple_target * atr_value
    if stop <= 0:
        return {"status": "excluded", "reason": "invalid_stop_price"}

    exit_bar = None
    exit_raw = None
    exit_reason = None
    bars_scanned = 0
    previous_open = None
    for bar in rows[entry_index:entry_index + config.max_holding_bars]:
        if previous_open is not None and bar.open_time_ms != previous_open + BAR_INTERVAL_MS:
            return {
                "status": "excluded",
                "reason": "missing_bar_in_holding_horizon",
                "bars_scanned": bars_scanned,
            }
        previous_open = bar.open_time_ms
        bars_scanned += 1

        if side == "LONG":
            stop_hit = bar.low <= stop
            target_hit = bar.high >= target
            stop_gap = bar.open <= stop
        else:
            stop_hit = bar.high >= stop
            target_hit = bar.low <= target
            stop_gap = bar.open >= stop
        if stop_hit:
            # A stop-market order cannot be assumed to fill at the stop after
            # the market has already gapped through it.
            exit_bar = bar
            exit_raw = bar.open if stop_gap else stop
            exit_reason = "stop_loss"
            break
        if target_hit:
            exit_bar, exit_raw, exit_reason = bar, target, "take_profit"
            break

    if exit_bar is None:
        last_index = min(entry_index + config.max_holding_bars - 1, len(rows) - 1)
        if last_index < entry_index or bars_scanned < config.max_holding_bars:
            return {"status": "excluded", "reason": "insufficient_matured_horizon"}
        exit_bar = rows[last_index]
        exit_raw = exit_bar.close
        exit_reason = "time_exit"

    exit_fill = float(exit_raw) * (1.0 - direction * slip)
    if exit_fill <= 0:
        return {"status": "excluded", "reason": "invalid_exit_fill"}

    if any(item.funding_time_ms == entry_bar.open_time_ms for item in funding):
        return {"status": "excluded", "reason": "funding_timing_ambiguous_at_entry"}

    applicable_funding = [
        item for item in funding
        if entry_bar.open_time_ms < item.funding_time_ms <= exit_bar.close_time_ms
    ]
    if exit_reason in ("stop_loss", "take_profit"):
        ambiguous = [
            item for item in applicable_funding
            if exit_bar.open_time_ms <= item.funding_time_ms <= exit_bar.close_time_ms
        ]
    else:
        ambiguous = [
            item for item in applicable_funding
            if item.funding_time_ms == exit_bar.close_time_ms
        ]
    if ambiguous:
        return {
            "status": "excluded",
            "reason": "funding_timing_ambiguous_inside_exit_bar",
            "ambiguous_funding_count": len(ambiguous),
        }

    gross_pnl = direction * (exit_fill - entry_fill)
    entry_fee = entry_fill * fee
    exit_fee = exit_fill * fee
    # Positive cashflow means funds received by the position; negative means paid.
    # On positive funding, LONG pays and SHORT receives.
    funding_cashflow = sum(
        -direction * item.funding_rate * item.mark_price
        for item in applicable_funding
    )
    net_pnl = gross_pnl - entry_fee - exit_fee + funding_cashflow
    risk_per_unit = stop_distance
    net_r = net_pnl / risk_per_unit
    return {
        "status": "labeled",
        "event_time_ms": int(event_time_ms),
        "entry_time_ms": entry_bar.open_time_ms,
        "exit_time_ms": exit_bar.close_time_ms,
        "side": side,
        "label": int(net_r > 0.0),
        "net_r": net_r,
        "gross_pnl_per_unit": gross_pnl,
        "entry_fee_per_unit": entry_fee,
        "exit_fee_per_unit": exit_fee,
        "funding_cashflow_per_unit": funding_cashflow,
        "funding_events_applied": len(applicable_funding),
        "entry_fill": entry_fill,
        "exit_fill": exit_fill,
        "stop_price": stop,
        "target_price": target,
        "exit_reason": exit_reason,
        "bars_scanned": bars_scanned,
        "config": asdict(config),
    }
