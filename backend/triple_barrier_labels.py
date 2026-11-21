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
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.atr_multiple_target <= self.atr_multiple_stop:
            raise ValueError("target multiple must exceed stop multiple")
        if isinstance(self.max_holding_bars, bool) or self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be a positive integer")
        for name in ("taker_fee_rate", "adverse_slippage_rate"):
            value = getattr(self, name)
            if value is None or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be explicitly set to a finite non-negative rate")


def _finite_positive(value: float, field: str) -> float:
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
        if bar.close_time_ms <= bar.open_time_ms:
            raise ValueError(f"bar[{index}] has invalid time bounds")
        if previous is not None and bar.open_time_ms <= previous:
            raise ValueError("bars must be strictly ordered and unique")
        previous = bar.open_time_ms
        values = [bar.open, bar.high, bar.low, bar.close]
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError(f"bar[{index}] contains non-finite OHLC")
        if min(values) <= 0 or bar.high < max(bar.open, bar.close, bar.low):
            raise ValueError(f"bar[{index}] has inconsistent OHLC")
        if bar.low > min(bar.open, bar.close):
            raise ValueError(f"bar[{index}] has inconsistent OHLC")
    return rows


def _validate_funding(events: Iterable[FundingEvent]) -> list[FundingEvent]:
    rows = sorted(list(events), key=lambda item: item.funding_time_ms)
    previous = None
    for index, event in enumerate(rows):
        if previous is not None and event.funding_time_ms <= previous:
            raise ValueError("funding timestamps must be unique")
        previous = event.funding_time_ms
        if not math.isfinite(float(event.funding_rate)):
            raise ValueError(f"funding[{index}] rate is non-finite")
        _finite_positive(event.mark_price, f"funding[{index}].mark_price")
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

    Entry is the next bar open. Barriers are evaluated on trade OHLC; if both
    barriers touch in one bar, SL wins. Funding settlements are included only
    when their timestamp is inside the position interval. If a settlement falls
    inside the exit bar and a barrier exits intrabar, ordering is unknowable
    from OHLC and the event is excluded rather than guessed.
    """
    config.validate()
    if not funding_coverage_verified:
        raise ValueError("funding coverage must be independently verified before labeling")
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
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
    slip = float(config.adverse_slippage_rate)
    fee = float(config.taker_fee_rate)
    direction = 1.0 if side == "LONG" else -1.0
    entry_fill = entry_bar.open * (1.0 + direction * slip)
    stop_distance = config.atr_multiple_stop * atr_value
    stop = entry_fill - direction * stop_distance
    target = entry_fill + direction * config.atr_multiple_target * atr_value
    if stop <= 0:
        return {"status": "excluded", "reason": "invalid_stop_price"}

    exit_bar = None
    exit_raw = None
    exit_reason = None
    bars_scanned = 0
    for bar in rows[entry_index:entry_index + config.max_holding_bars]:
        bars_scanned += 1
        if side == "LONG":
            stop_hit = bar.low <= stop
            target_hit = bar.high >= target
        else:
            stop_hit = bar.high >= stop
            target_hit = bar.low <= target
        if stop_hit:
            exit_bar, exit_raw, exit_reason = bar, stop, "stop_loss"
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

    applicable_funding = [
        item for item in funding
        if entry_bar.open_time_ms <= item.funding_time_ms <= exit_bar.close_time_ms
    ]
    if exit_reason in ("stop_loss", "take_profit"):
        ambiguous = [
            item for item in applicable_funding
            if exit_bar.open_time_ms < item.funding_time_ms <= exit_bar.close_time_ms
        ]
        if ambiguous:
            return {
                "status": "excluded",
                "reason": "funding_timing_ambiguous_inside_intrabar_exit",
                "ambiguous_funding_count": len(ambiguous),
            }

    gross_pnl = direction * (exit_fill - entry_fill)
    entry_fee = entry_fill * fee
    exit_fee = exit_fill * fee
    funding_cashflow = sum(
        direction * item.funding_rate * item.mark_price
        for item in applicable_funding
    )
    net_pnl = gross_pnl - entry_fee - exit_fee - funding_cashflow
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
