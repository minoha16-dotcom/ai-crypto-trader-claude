from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.vike_exploratory_train import BAR_MS
from scripts.vike_v31_research_pipeline import (
    apply_overlap_control,
    build_opportunity_events,
    label_opportunity,
    load_funding_attestation,
    load_liquidation_manifest,
    run_walk_forward,
)


def bar(index: int, *, close: float = 100.0, high: float = 101.0,
        low: float = 99.0, volume: float = 10.0) -> dict:
    start = index * BAR_MS
    return {
        "t": start, "close_t": start + BAR_MS - 1,
        "o": close, "h": max(high, close), "l": min(low, close),
        "c": close, "v": volume,
    }


def test_generator_creates_direction_neutral_long_event_without_labeling():
    rows = [bar(i) for i in range(20)]
    rows.append(bar(20, close=103, high=104, low=102))
    rows.append(bar(21, close=103, high=104, low=102))
    events = build_opportunity_events("BTCUSDT", rows)
    assert len(events) == 1
    assert events[0]["side"] == "LONG"
    assert events[0]["signal_time_ms"] == rows[20]["close_t"]
    assert events[0]["entry_time_ms"] == rows[21]["t"]
    assert "label" not in events[0]
    assert events[0]["event_status"] == "GENERATED"


def test_generator_creates_short_event():
    rows = [bar(i) for i in range(20)]
    rows.extend([
        bar(20, close=97, high=98, low=96),
        bar(21, close=97, high=98, low=96),
    ])
    events = build_opportunity_events("ETHUSDT", rows)
    assert len(events) == 1
    assert events[0]["side"] == "SHORT"


def test_overlap_control_is_fixed_horizon_and_per_symbol():
    rows = [
        {"symbol": "BTCUSDT", "signal_time_ms": 0},
        {"symbol": "BTCUSDT", "signal_time_ms": 47 * BAR_MS},
        {"symbol": "BTCUSDT", "signal_time_ms": 100 * BAR_MS},
        {"symbol": "ETHUSDT", "signal_time_ms": 47 * BAR_MS},
    ]
    result = apply_overlap_control(rows)
    by_key = {(row["symbol"], row["signal_time_ms"]): row for row in result}
    assert by_key[("BTCUSDT", 0)]["overlap_status"] == "ELIGIBLE_NON_OVERLAPPING"
    assert by_key[("BTCUSDT", 47 * BAR_MS)]["overlap_status"] == "EXCLUDED_FIXED_HORIZON_COOLDOWN"
    assert by_key[("BTCUSDT", 100 * BAR_MS)]["overlap_status"] == "ELIGIBLE_NON_OVERLAPPING"
    assert by_key[("ETHUSDT", 47 * BAR_MS)]["overlap_status"] == "ELIGIBLE_NON_OVERLAPPING"


def test_funding_attestation_must_be_verified_and_cover_exact_symbol_month_grid(tmp_path: Path):
    path = tmp_path / "funding.json"
    path.write_text(json.dumps({
        "schema_version": 1, "coverage_status": "NOT_VERIFIED",
        "evidence_sha256": "a" * 64, "symbol_months": [],
    }), encoding="utf-8")
    index, status = load_funding_attestation(path, ["BTCUSDT"])
    assert index == {}
    assert status == "NOT_VERIFIED"


def test_missing_liquidation_manifest_fails_closed():
    rows, status = load_liquidation_manifest(None, ["BTCUSDT"])
    assert rows == []
    assert status == "NOT_VERIFIED"


def test_labeling_fails_closed_when_funding_is_not_verified():
    event = {
        "symbol": "BTCUSDT", "signal_time_ms": BAR_MS - 1,
        "entry_time_ms": BAR_MS, "side": "LONG", "atr": 1.0,
        "overlap_status": "ELIGIBLE_NON_OVERLAPPING",
    }
    result = label_opportunity(
        event, [], {}, [], False, [], False, 5.0, 2.0,
    )
    assert result == {"status": "excluded", "reason": "funding_coverage_not_verified"}


def test_walk_forward_does_not_train_without_verified_labels():
    result = run_walk_forward([])
    assert result["status"] == "BLOCKED_NO_VERIFIED_LABELS"
    assert result["folds"] == []
    assert result["lightgbm"] == "NOT_RUN"
