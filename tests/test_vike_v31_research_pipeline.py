from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.binance_usdm_liquidation import MaintenanceBracket
from scripts.vike_exploratory_train import BAR_MS
from scripts.vike_v31_research_pipeline import (
    apply_overlap_control,
    build_opportunity_events,
    label_opportunity,
    load_funding_attestation,
    load_liquidation_manifest,
    run_walk_forward,
    _read_funding,
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


def test_generator_retains_terminal_event_without_next_bar():
    rows = [bar(i) for i in range(20)]
    rows.append(bar(20, close=103, high=104, low=102))
    events = build_opportunity_events("BTCUSDT", rows)
    assert len(events) == 1
    assert events[0]["entry_time_ms"] is None


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
        {"symbol": "BTCUSDT", "signal_time_ms": 48 * BAR_MS},
        {"symbol": "BTCUSDT", "signal_time_ms": 100 * BAR_MS},
        {"symbol": "ETHUSDT", "signal_time_ms": 47 * BAR_MS},
    ]
    result = apply_overlap_control(rows)
    by_key = {(row["symbol"], row["signal_time_ms"]): row for row in result}
    assert by_key[("BTCUSDT", 0)]["overlap_status"] == "ELIGIBLE_NON_OVERLAPPING"
    assert by_key[("BTCUSDT", 47 * BAR_MS)]["overlap_status"] == "EXCLUDED_FIXED_HORIZON_COOLDOWN"
    assert by_key[("BTCUSDT", 48 * BAR_MS)]["overlap_status"] == "ELIGIBLE_NON_OVERLAPPING"
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


def test_empty_funding_file_is_rejected(tmp_path: Path):
    path = tmp_path / "funding.csv"
    path.write_text("funding_time,funding_rate,mark_price" + chr(10), encoding="utf-8")
    with pytest.raises(ValueError, match="no valid records"):
        _read_funding(path, 0, 10_000_000)


def test_missing_liquidation_manifest_fails_closed():
    rows, status = load_liquidation_manifest(None, ["BTCUSDT"])
    assert rows == []
    assert status == "NOT_VERIFIED"


def test_liquidation_manifest_requires_hash_bound_evidence_artifacts(tmp_path: Path):
    evidence = tmp_path / "exchange-tier-source.txt"
    snapshot = tmp_path / "position-snapshot.json"
    evidence.write_text("official historical tier evidence", encoding="utf-8")
    snapshot.write_text('{"quantity": 1, "wallet": 100}', encoding="utf-8")
    digest = lambda file: hashlib.sha256(file.read_bytes()).hexdigest()
    from scripts.vike_exploratory_train import MONTHS, month_bounds
    start, _ = month_bounds(MONTHS[0])
    _, end = month_bounds(MONTHS[-1])
    manifest = tmp_path / "liquidation.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "status": "VERIFIED",
        "evidence_sha256": digest(evidence),
        "evidence_artifact_path": evidence.name,
        "reviewed_by": "independent-review",
        "reviewed_at_utc": "2026-11-23T00:00:00Z",
        "positions": [{
            "symbol": "BTCUSDT",
            "valid_from_ms": start,
            "valid_to_ms": end,
            "margin_mode": "ISOLATED",
            "position_mode": "ONE_WAY",
            "contract_type": "USDT_LINEAR",
            "quantity": 1.0,
            "isolated_wallet_balance": 100.0,
            "no_external_flows": True,
            "snapshot_sha256": digest(snapshot),
            "snapshot_artifact_path": snapshot.name,
            "brackets": [{
                "notional_floor": 0.0,
                "notional_cap": 1_000_000_000_000.0,
                "maint_margin_ratio": 0.004,
                "cum": 0.0,
            }],
        }],
    }), encoding="utf-8")
    rows, status = load_liquidation_manifest(manifest, ["BTCUSDT"])
    assert status == "VERIFIED"
    assert len(rows) == 1


def test_liquidation_manifest_rejects_unbound_evidence_hash(tmp_path: Path):
    manifest = tmp_path / "liquidation.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "status": "VERIFIED",
        "evidence_sha256": "a" * 64,
        "reviewed_by": "independent-review",
        "reviewed_at_utc": "2026-11-23T00:00:00Z",
        "positions": [],
    }), encoding="utf-8")
    rows, status = load_liquidation_manifest(manifest, ["BTCUSDT"])
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


def test_triple_barrier_label_path_uses_mark_and_liquidation_verification():
    segment = [bar(i) for i in range(69)]
    mark_map = {
        row["t"]: {
            "t": row["t"], "close_t": row["close_t"],
            "mark_o": row["o"], "mark_h": row["h"],
            "mark_l": row["l"], "mark_c": row["c"],
        }
        for row in segment
    }
    event = {
        "symbol": "BTCUSDT",
        "signal_time_ms": segment[20]["close_t"],
        "entry_time_ms": segment[21]["t"],
        "side": "LONG",
        "atr": 1.0,
        "overlap_status": "ELIGIBLE_NON_OVERLAPPING",
    }
    positions = [{
        "symbol": "BTCUSDT",
        "valid_from_ms": 0,
        "valid_to_ms": 10_000_000_000,
        "quantity": 1.0,
        "isolated_wallet_balance": 10.0,
        "_brackets": [MaintenanceBracket(0.0, 1_000.0, 0.004, 0.0)],
    }]
    result = label_opportunity(
        event, segment, mark_map, [], True, positions, True, 5.0, 2.0,
    )
    assert result["status"] == "labeled"
    assert result["exit_reason"] == "time_exit"
    assert result["funding_cashflow_per_unit"] == 0.0


def test_walk_forward_does_not_train_without_verified_labels():
    result = run_walk_forward([])
    assert result["status"] == "BLOCKED_NO_VERIFIED_LABELS"
    assert result["folds"] == []
    assert result["lightgbm"] == "NOT_RUN"
