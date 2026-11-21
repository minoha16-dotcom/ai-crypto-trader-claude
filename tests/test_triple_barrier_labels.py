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
        event_time_ms=rows[0].open_time_ms - 1,
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
    settlement = FundingEvent(900_001, 0.001, 100)
    long = run("LONG", [bar(900_000, 100, 100, 100, 100)] + [
        bar((i + 2) * 900_000) for i in range(47)
    ], [settlement])
    short = run("SHORT", [bar(900_000, 100, 100, 100, 100)] + [
        bar((i + 2) * 900_000) for i in range(47)
    ], [settlement])
    assert long["funding_cashflow_per_unit"] == pytest.approx(-0.1)
    assert short["funding_cashflow_per_unit"] == pytest.approx(0.1)


def test_missing_funding_coverage_fails_closed():
    with pytest.raises(ValueError, match="coverage"):
        label_event(
            event_time_ms=899_999, side="LONG", atr=1,
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
    assert result["reason"] == "funding_timing_ambiguous_inside_exit_bar"


def test_invalid_ohlc_and_duplicate_timestamps_are_rejected():
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        run("LONG", [bar(900_000, 100, 99, 98, 100)])
    with pytest.raises(ValueError, match="strictly ordered"):
        run("LONG", [bar(900_000), bar(900_000)])


def test_funding_duplicates_are_rejected():
    f = FundingEvent(900_000, 0.001, 100)
    with pytest.raises(ValueError, match="unique"):
        run("LONG", [bar(900_000)], [f, f])



def test_long_stop_gap_fills_at_open_not_stale_stop():
    rows = [bar(900_000)] + [bar(1_800_000, 97, 98, 96, 97)] + [
        bar((i + 3) * 900_000) for i in range(46)
    ]
    result = run("LONG", rows)
    assert result["exit_reason"] == "stop_loss"
    assert result["exit_fill"] == pytest.approx(97 * (1 - 0.0005))
    assert result["exit_fill"] < result["stop_price"]


def test_short_stop_gap_fills_at_open_not_stale_stop():
    rows = [bar(900_000)] + [bar(1_800_000, 103, 104, 102, 103)] + [
        bar((i + 3) * 900_000) for i in range(46)
    ]
    result = run("SHORT", rows)
    assert result["exit_reason"] == "stop_loss"
    assert result["exit_fill"] == pytest.approx(103 * (1 + 0.0005))
    assert result["exit_fill"] > result["stop_price"]


def test_funding_inside_time_exit_bar_is_applied():
    rows = [bar((i + 1) * 900_000) for i in range(48)]
    settlement = FundingEvent(48 * 900_000 + 100_000, 0.001, 100)
    result = run("LONG", rows, [settlement])
    assert result["status"] == "labeled"
    assert result["funding_events_applied"] == 1
    assert result["funding_cashflow_per_unit"] == pytest.approx(-0.1)


def test_funding_exactly_at_entry_timestamp_is_excluded():
    rows = [bar(900_000)] + [bar((i + 2) * 900_000) for i in range(47)]
    result = run("LONG", rows, [FundingEvent(900_000, 0.001, 100)])
    assert result["status"] == "excluded"
    assert result["reason"] == "funding_timing_ambiguous_at_entry"


def test_missing_bar_inside_holding_horizon_is_excluded():
    rows = [bar((i + 1) * 900_000) for i in range(48)]
    rows.pop(10)
    result = run("LONG", rows)
    assert result["status"] == "excluded"
    assert result["reason"] == "missing_bar_in_holding_horizon"


def test_entry_requires_immediate_next_bar_after_signal_close():
    result = label_event(
        event_time_ms=899_999, side="LONG", atr=1,
        bars=[bar(1_800_000)], funding_events=[],
        funding_coverage_verified=True, config=cfg(),
    )
    assert result == {"status": "excluded", "reason": "missing_immediate_next_bar"}


def test_partial_15m_bar_is_rejected():
    partial = Bar(900_000, 1_700_000, 100, 100, 100, 100)
    with pytest.raises(ValueError, match="complete 15-minute"):
        run("LONG", [partial])


def test_fee_and_slippage_rates_must_be_below_one():
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        run("LONG", [bar(900_000)], taker_fee_rate=1.0)
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        run("LONG", [bar(900_000)], adverse_slippage_rate=1.0)


def test_stop_loss_pnl_and_r_match_independent_arithmetic():
    rows = [
        bar(900_000, 100, 100, 100, 100),
        bar(1_800_000, 100, 100, 98, 99),
    ]
    result = run("LONG", rows)
    expected_entry = 100 * 1.0005
    expected_exit = 98.5 * 0.9995
    expected_gross = expected_exit - expected_entry
    expected_fees = expected_entry * 0.001 + expected_exit * 0.001
    expected_net = expected_gross - expected_fees
    assert result["entry_fill"] == pytest.approx(expected_entry)
    assert result["exit_fill"] == pytest.approx(expected_exit)
    assert result["gross_pnl_per_unit"] == pytest.approx(expected_gross)
    assert result["entry_fee_per_unit"] + result["exit_fee_per_unit"] == pytest.approx(expected_fees)
    assert result["net_r"] == pytest.approx(expected_net / 1.5)


def test_take_profit_pnl_and_r_match_independent_arithmetic():
    rows = [
        bar(900_000, 100, 100, 100, 100),
        bar(1_800_000, 100, 104, 100, 103.5),
    ]
    result = run("LONG", rows)
    expected_entry = 100 * 1.0005
    expected_exit = (expected_entry + 3.0) * 0.9995
    expected_gross = expected_exit - expected_entry
    expected_fees = expected_entry * 0.001 + expected_exit * 0.001
    expected_net = expected_gross - expected_fees
    assert result["exit_reason"] == "take_profit"
    assert result["net_r"] == pytest.approx(expected_net / 1.5)


def test_boolean_ohlc_and_funding_values_are_rejected():
    with pytest.raises(ValueError, match="OHLC values must be numeric"):
        run("LONG", [Bar(900_000, 1_799_999, True, 100, 99, 100)])
    with pytest.raises(ValueError, match="rate must be numeric"):
        run("LONG", [bar(900_000)], [FundingEvent(900_001, True, 100)])
    with pytest.raises(ValueError, match="numeric finite positive"):
        run("LONG", [bar(900_000)], [FundingEvent(900_001, 0.001, True)])


def test_event_time_must_be_aligned_to_15m_close():
    with pytest.raises(ValueError, match="align to a UTC 15-minute"):
        label_event(
            event_time_ms=900_000, side="LONG", atr=1,
            bars=[bar(900_001)], funding_events=[],
            funding_coverage_verified=True, config=cfg(),
        )


def test_funding_cashflow_is_added_to_net_pnl_with_correct_sign():
    rows = [bar((i + 1) * 900_000) for i in range(48)]
    settlement = FundingEvent(1_900_000, 0.001, 100)
    long_without_funding = run("LONG", rows)
    short_without_funding = run("SHORT", rows)
    long = run("LONG", rows, [settlement])
    short = run("SHORT", rows, [settlement])
    assert long["funding_cashflow_per_unit"] == pytest.approx(-0.1)
    assert short["funding_cashflow_per_unit"] == pytest.approx(0.1)
    assert long["net_r"] == pytest.approx(long_without_funding["net_r"] - 0.1 / 1.5)
    assert short["net_r"] == pytest.approx(short_without_funding["net_r"] + 0.1 / 1.5)
