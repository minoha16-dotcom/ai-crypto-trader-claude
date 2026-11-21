from __future__ import annotations

import pytest

from backend.triple_barrier_labels import Bar, FundingEvent, LabelConfig, label_event


def cfg(**kwargs):
    values = {"taker_fee_rate": 0.001, "adverse_slippage_rate": 0.0005}
    values.update(kwargs)
    return LabelConfig(**values)


def bar(t, o=100, h=100, l=100, c=100):
    return Bar(t, t + 899_999, o, h, l, c)


def run(side, rows, funding=(), **kwargs):
    return label_event(
        event_time_ms=0,
        side=side,
        atr=1,
        bars=rows,
        funding_events=funding,
        funding_coverage_verified=True,
        config=cfg(**kwargs),
    )


def test_long_stop_wins_when_both_barriers_touch_same_bar():
    result = run("LONG", [bar(900_000, 100, 104, 96, 100)])
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "stop_loss"
    assert result["label"] == 0


def test_short_stop_wins_when_both_barriers_touch_same_bar():
    result = run("SHORT", [bar(900_000, 100, 104, 96, 100)])
    assert result["exit_reason"] == "stop_loss"


def test_long_take_profit_and_short_take_profit():
    long = run("LONG", [bar(900_000, 100, 104, 100, 103)])
    short = run("SHORT", [bar(900_000, 100, 100, 96, 97)])
    assert long["exit_reason"] == "take_profit"
    assert short["exit_reason"] == "take_profit"
    assert long["label"] == short["label"] == 1


def test_time_exit_requires_full_48_bar_maturity():
    rows = [bar((i + 1) * 900_000) for i in range(48)]
    result = run("LONG", rows)
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "time_exit"
    short = run("LONG", rows[:47])
    assert short == {"status": "excluded", "reason": "insufficient_matured_horizon"}


def test_entry_is_next_bar_open_after_signal():
    result = run("LONG", [bar(900_000, 101, 101, 101, 101)] + [
        bar((i + 2) * 900_000) for i in range(47)
    ])
    assert result["entry_fill"] == pytest.approx(101 * 1.0005)


def test_funding_positive_rate_costs_long_and_benefits_short():
    settlement = FundingEvent(900_000, 0.001, 100)
    long = run("LONG", [bar(900_000, 100, 100, 100, 100)] + [
        bar((i + 2) * 900_000) for i in range(47)
    ], [settlement])
    short = run("SHORT", [bar(900_000, 100, 100, 100, 100)] + [
        bar((i + 2) * 900_000) for i in range(47)
    ], [settlement])
    assert long["funding_cashflow_per_unit"] == pytest.approx(0.1)
    assert short["funding_cashflow_per_unit"] == pytest.approx(-0.1)


def test_missing_funding_coverage_fails_closed():
    with pytest.raises(ValueError, match="coverage"):
        label_event(
            event_time_ms=0, side="LONG", atr=1,
            bars=[bar(900_000)], funding_events=[],
            funding_coverage_verified=False, config=cfg(),
        )


def test_costs_must_be_explicit():
    with pytest.raises(ValueError, match="taker_fee_rate"):
        run("LONG", [bar(900_000)], taker_fee_rate=None)


def test_funding_settlement_inside_intrabar_exit_is_excluded_as_ambiguous():
    rows = [bar(900_000, 100, 104, 96, 100)]
    settlement = FundingEvent(1_000_000, 0.001, 100)
    result = run("LONG", rows, [settlement])
    assert result["status"] == "excluded"
    assert result["reason"] == "funding_timing_ambiguous_inside_intrabar_exit"


def test_invalid_ohlc_and_duplicate_timestamps_are_rejected():
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        run("LONG", [bar(900_000, 100, 99, 98, 100)])
    with pytest.raises(ValueError, match="strictly ordered"):
        run("LONG", [bar(900_000), bar(900_000)])


def test_funding_duplicates_are_rejected():
    f = FundingEvent(900_000, 0.001, 100)
    with pytest.raises(ValueError, match="unique"):
        run("LONG", [bar(900_000)], [f, f])
