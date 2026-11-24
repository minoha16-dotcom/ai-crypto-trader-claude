from types import SimpleNamespace

import pytest

from scripts.vike_exploratory_train import BAR_MS
from scripts.vike_v31_price_only_labels import label_price_only_event


def make_case(*, first_mark=None, funding=(), side="LONG"):
    start = 100 * BAR_MS
    trade = []
    marks = {}
    for index in range(48):
        timestamp = start + index * BAR_MS
        row = {
            "t": timestamp, "close_t": timestamp + BAR_MS - 1,
            "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0,
        }
        trade.append(row)
        mark = first_mark if index == 0 and first_mark is not None else {
            "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0,
        }
        marks[timestamp] = {
            "t": timestamp, "close_t": timestamp + BAR_MS - 1,
            "mark_o": mark["o"], "mark_h": mark["h"],
            "mark_l": mark["l"], "mark_c": mark["c"],
        }
    event = {
        "symbol": "BTCUSDT",
        "signal_time_ms": start - 1,
        "entry_time_ms": start,
        "side": side,
        "atr": 2.0,
        "overlap_status": "ELIGIBLE_NON_OVERLAPPING",
    }
    return event, trade, marks, list(funding)


def test_mark_price_stop_wins_when_both_barriers_touch():
    event, trade, marks, funding = make_case(
        first_mark={"o": 100.0, "h": 107.0, "l": 96.0, "c": 100.0}
    )
    result = label_price_only_event(event, trade, marks, funding, 0.0, 0.0)
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "stop_loss"
    assert result["label"] == 0
    assert result["net_r"] == pytest.approx(-1.0)
    assert result["barrier_trigger_source"] == "MARK_PRICE_OHLC"


def test_mark_price_target_hit_creates_positive_label():
    event, trade, marks, funding = make_case(
        first_mark={"o": 100.0, "h": 107.0, "l": 99.0, "c": 105.0}
    )
    result = label_price_only_event(event, trade, marks, funding, 0.0, 0.0)
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "take_profit"
    assert result["label"] == 1
    assert result["net_r"] == pytest.approx(2.0)


def test_funding_settlement_inside_position_excludes_event():
    event, trade, marks, _ = make_case()
    funding = [SimpleNamespace(funding_time_ms=event["entry_time_ms"] + 1)]
    result = label_price_only_event(event, trade, marks, funding, 0.0, 0.0)
    assert result["status"] == "excluded"
    assert result["reason"] == "funding_settlement_during_open_position"


def test_time_exit_uses_48th_mark_close_and_no_funding_cashflow():
    event, trade, marks, funding = make_case()
    result = label_price_only_event(event, trade, marks, funding, 0.0, 0.0)
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "time_exit"
    assert result["bars_scanned"] == 48
    assert result["funding_cashflow_per_unit"] == 0.0
    assert result["funding_events_applied"] == 0


def test_missing_mark_bar_is_excluded():
    event, trade, marks, funding = make_case()
    marks.pop(event["entry_time_ms"])
    result = label_price_only_event(event, trade, marks, funding)
    assert result == {"status": "excluded", "reason": "mark_price_bar_missing"}


def test_missing_matured_horizon_is_excluded():
    event, trade, marks, funding = make_case()
    result = label_price_only_event(event, trade[:20], marks, funding)
    assert result == {"status": "excluded", "reason": "insufficient_matured_horizon"}


def test_overlap_excluded_before_labeling():
    event, trade, marks, funding = make_case()
    event["overlap_status"] = "EXCLUDED_FIXED_HORIZON_COOLDOWN"
    result = label_price_only_event(event, trade, marks, funding)
    assert result["status"] == "excluded"
    assert result["reason"] == "EXCLUDED_FIXED_HORIZON_COOLDOWN"


def test_short_side_stop_is_checked_on_mark_high():
    event, trade, marks, funding = make_case(
        first_mark={"o": 100.0, "h": 104.0, "l": 90.0, "c": 100.0},
        side="SHORT",
    )
    result = label_price_only_event(event, trade, marks, funding, 0.0, 0.0)
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "stop_loss"
    assert result["label"] == 0
