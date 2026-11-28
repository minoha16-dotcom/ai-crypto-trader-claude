import json
import math

import pytest

from backend.ml_shadow import (
    FEATURE_NAMES, RAW_FEATURE_NAMES, ShadowModelError, direction_aligned_features,
    load_artifact, predict_probability, run_shadow_prediction,
    validate_causal_timestamps, validate_features,
)

def valid_artifact():
    return {
        "schema_version": 1,
        "artifact_status": "SHADOW_ONLY",
        "model_type": "logistic_regression",
        "model_version": "test-only",
        "feature_names": list(FEATURE_NAMES),
        "market_scope": "BINANCE_USDM_PERPETUAL",
        "timeframe": "15m",
        "training_cutoff_utc": "2026-05-01T00:00:00Z",
        "dataset_sha256": "a" * 64,
        "research_report_sha256": "b" * 64,
        "standard_scaler": {"mean": [0.0] * 14, "scale": [1.0] * 14},
        "logistic_regression": {"coef": [0.0] * 14, "intercept": 0.0},
    }

def valid_payload():
    signal = 1_800_000_000_000
    signal = signal - (signal % 900_000) + 899_999
    hour_close = signal - (signal % 3_600_000) + 3_599_999
    if hour_close > signal:
        hour_close -= 3_600_000
    four_hour_close = signal - (signal % 14_400_000) + 14_399_999
    if four_hour_close > signal:
        four_hour_close -= 14_400_000
    return {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "15m",
        "market_scope": "BINANCE_USDM_PERPETUAL",
        "signal_time_ms": signal, "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": hour_close, "htf_4h_close_time_ms": four_hour_close,
        "raw_features": {**{name: 0.0 for name in RAW_FEATURE_NAMES}, "atr_pct": 0.01,
                        "donchian_width_pct": 0.02, "rsi14": 0.5, "channel_position": 0.5},
    }

def test_feature_schema_requires_exact_names_and_ordered_contract():
    features = {name: 0.0 for name in FEATURE_NAMES}
    assert validate_features(features) == [0.0] * 14
    features.pop(FEATURE_NAMES[0])
    with pytest.raises(ShadowModelError, match="schema mismatch"):
        validate_features(features)

def test_nonfinite_feature_rejected():
    features = {name: 0.0 for name in FEATURE_NAMES}
    features[FEATURE_NAMES[0]] = float("nan")
    with pytest.raises(ShadowModelError, match="finite"):
        validate_features(features)

def test_timestamp_future_and_wrong_boundary_rejected():
    p = valid_payload()
    validate_causal_timestamps(p["signal_time_ms"], p["feature_available_time_ms"],
                               p["htf_1h_close_time_ms"], p["htf_4h_close_time_ms"])
    with pytest.raises(ShadowModelError):
        validate_causal_timestamps(p["signal_time_ms"] + 1, p["feature_available_time_ms"],
                                   p["htf_1h_close_time_ms"], p["htf_4h_close_time_ms"])

def test_artifact_loader_requires_shadow_status_and_exact_schema(tmp_path):
    artifact = valid_artifact()
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    loaded, digest = load_artifact(path)
    assert loaded["model_version"] == "test-only"
    assert len(digest) == 64
    artifact["feature_names"][0] = "wrong_feature"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ShadowModelError, match="feature order"):
        load_artifact(path)

def test_logistic_probability_with_zero_parameters_is_half():
    assert predict_probability(valid_artifact(), {name: 1.0 for name in FEATURE_NAMES}) == pytest.approx(0.5)

def test_direction_alignment_mirrors_short_and_preserves_hour_features():
    raw = {name: 0.0 for name in RAW_FEATURE_NAMES}
    raw.update({"atr_pct": 0.01, "donchian_width_pct": 0.02, "channel_position": 1.2,
                "volume_ratio": 3.0, "rsi14": 0.7, "hour_sin": 0.5, "hour_cos": -0.5,
                "ret_1": 0.01, "htf_1h_ret_1": 0.02})
    long = direction_aligned_features(raw, "LONG")
    short = direction_aligned_features(raw, "SHORT")
    assert long["dir_ret_1"] == -short["dir_ret_1"]
    assert long["dir_rsi14"] == pytest.approx(0.7)
    assert short["dir_rsi14"] == pytest.approx(0.3)
    assert long["hour_sin"] == short["hour_sin"] == 0.5
    assert long["dir_breakout_distance"] == pytest.approx(0.2)
    assert short["dir_breakout_distance"] == pytest.approx(-1.2)

def test_missing_artifact_fails_closed(tmp_path):
    p = valid_payload()
    with pytest.raises(ShadowModelError, match="missing"):
        run_shadow_prediction(p, tmp_path / "absent.json")

def test_shadow_prediction_is_explicitly_diagnostic(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps(valid_artifact()), encoding="utf-8")
    result = run_shadow_prediction(valid_payload(), path)
    assert result["diagnostic_only"] is True
    assert result["trade_decision_changed"] is False
    assert result["probability"] == pytest.approx(0.5)
