import hashlib
import math

import pytest

from backend.advisor_model_validation import (
    DEFAULT_POLICY,
    bootstrap_uplift_interval,
    evaluate_approval,
    fit_ridge,
    predict_ridge,
    train_validate_group,
    walk_forward_predict,
)


def _features(index):
    return {
        "trend_composite": 0.8 if index % 2 else -0.6,
        "rsi14": 60.0 if index % 2 else 38.0,
        "adx14": 30.0 + index % 10,
        "volume_ratio": 1.2 + (index % 5) * 0.1,
        "atr_pct": 0.5 + (index % 7) * 0.05,
    }


def _samples(count, start_ms=1_760_000_000_000):
    rows = []
    for index in range(count):
        signal_ms = start_ms + index * 60 * 60 * 1000
        # Outcomes mature before the next test partition; real-world overlap is
        # separately guarded by the explicit exit_timestamp cutoff.
        r_value = 0.65 if index % 2 else -0.15
        rows.append({
            "sample_id": f"trade-{index}",
            "signal_timestamp": signal_ms,
            "exit_timestamp": signal_ms + 10 * 60 * 1000,
            "features": _features(index),
            "r_multiple": r_value,
        })
    return rows


def _manifest(start_ms):
    return {
        "source_provider": "Hyperliquid public fills + UnderHyper leaderboard",
        "source_url": "https://underhyper.com/data/leaderboard/",
        "source_license": "CC BY 4.0; attribute UnderHyper",
        "source_snapshot_utc": "2026-11-01T00:00:00+00:00",
        "source_snapshot_sha256": "a" * 64,
        "cohort_selection_as_of_utc": "2026-11-01T00:00:00+00:00",
        "data_start_utc": "2026-11-02T00:00:00+00:00",
        "data_end_utc": "2026-11-15T00:00:00+00:00",
        "model_version": "1.0.0",
        "feature_schema_version": "advisor-r5-v1",
        "code_sha": "abcdef1234567",
        "backtest_config": {
            "risk_unit_definition": "net PnL divided by standardized 2xATR-or-0.5pct initial risk",
            "fee_model": "actual Hyperliquid fill fees plus configured residual fees",
            "slippage_model": "conservative 2 bps each side",
            "slippage_bps": 2.0,
            "fee_rate": 0.0006,
            "funding_included": True,
            "entry_rule": "first observed public open fill",
            "exit_rule": "first full close of the reconstructed position",
        },
    }


def _successful_prediction_rows():
    rows = []
    for fold in range(1, 4):
        for index in range(40):
            predicted = float(40 - index)
            actual = 1.0 if index < 14 else -0.25
            rows.append({
                "sample_id": f"f{fold}-{index}",
                "fold_id": fold,
                "predicted_r_multiple": predicted,
                "r_multiple": actual,
            })
    return rows


def test_ridge_model_fits_and_predicts_finite_r_multiple():
    samples = _samples(80)
    model = fit_ridge(samples, alpha=1.0)
    estimate = predict_ridge(model, samples[0]["features"])

    assert model["model_family"] == "ridge_linear_r"
    assert model["feature_schema_version"] == "advisor-r5-v1"
    assert math.isfinite(estimate)
    assert set(model["coefficients"]) == {
        "trend_composite", "rsi14", "adx14", "volume_ratio", "atr_pct"
    }


def test_walk_forward_uses_only_fully_matured_pre_test_labels():
    samples = _samples(320)
    # Make the last 80 pre-test labels close after the first test begins. The
    # walk-forward runner must not silently include them as training examples.
    first_test_start = samples[200]["signal_timestamp"]
    for row in samples[120:200]:
        row["exit_timestamp"] = first_test_start + 1

    with pytest.raises(ValueError, match="fully matured training labels"):
        walk_forward_predict(
            samples,
            min_training_samples=200,
            min_folds=3,
            max_folds=3,
            min_test_samples_per_fold=20,
        )


def test_walk_forward_produces_at_least_three_chronological_oos_folds():
    result = walk_forward_predict(
        _samples(320),
        min_training_samples=200,
        min_folds=3,
        max_folds=3,
        min_test_samples_per_fold=20,
    )

    assert result["fold_count"] == 3
    assert result["min_training_samples_per_fold"] >= 200
    assert len(result["oos_predictions"]) == 120
    assert result["folds"][0]["test_end_utc"] < result["folds"][1]["test_start_utc"]
    assert result["folds"][1]["test_end_utc"] < result["folds"][2]["test_start_utc"]


def test_bootstrap_uplift_is_reproducible_and_positive_for_separated_scores():
    rows = _successful_prediction_rows()
    ci1 = bootstrap_uplift_interval(rows, resamples=500, seed=7)
    ci2 = bootstrap_uplift_interval(rows, resamples=500, seed=7)

    assert ci1 == ci2
    assert ci1[0] > 0


def test_approval_requires_provenance_and_non_lookahead_cohort_cutoff():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
        "excluded_samples": 4,
    }
    walk_forward = {
        "min_training_samples_per_fold": 250,
        "fold_count": 3,
        "oos_signals": 120,
        "folds": [{"test_samples": 40, "training_samples": 250}] * 3,
        "oos_predictions": _successful_prediction_rows(),
    }
    manifest = _manifest(0)

    approved = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=manifest,
        dataset_sha256="b" * 64,
    )
    assert approved["approved"] is True
    assert approved["model_status"] == "APPROVED"
    assert approved["metrics"]["uplift"] > 0
    assert approved["metrics"]["uplift_confidence_interval_95"][0] > 0
    assert approved["metrics"]["top_tercile_samples"] >= 30

    leaking_manifest = dict(manifest)
    leaking_manifest["cohort_selection_as_of_utc"] = "2026-11-10T00:00:00+00:00"
    rejected = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=leaking_manifest,
        dataset_sha256="b" * 64,
    )
    assert rejected["approved"] is False
    assert rejected["model_status"] == "DISABLED"
    assert any(reason["code"] == "COHORT_SELECTION_LOOKAHEAD" for reason in rejected["rejection_reasons"])


def test_approval_rejects_top_tercile_without_positive_lower_confidence_bound():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
    }
    rows = []
    for fold in range(1, 4):
        for index in range(40):
            rows.append({
                "sample_id": f"f{fold}-{index}",
                "fold_id": fold,
                "predicted_r_multiple": float(index),
                "r_multiple": 0.0,
            })
    walk_forward = {
        "min_training_samples_per_fold": 250,
        "fold_count": 3,
        "oos_signals": 120,
        "folds": [{"test_samples": 40, "training_samples": 250}] * 3,
        "oos_predictions": rows,
    }
    result = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=_manifest(0),
        dataset_sha256="c" * 64,
        policy={"bootstrap_resamples": 500},
    )

    assert result["approved"] is False
    assert any(reason["code"] == "UPLIFT_CI_INCLUDES_ZERO" for reason in result["rejection_reasons"])


def test_insufficient_per_group_training_count_never_approves():
    group = {
        "exchange": "hyperliquid",
        "symbol": "ETH/USDC",
        "market_type": "futures",
        "timeframe": "4h",
    }
    walk_forward = {
        "min_training_samples_per_fold": 199,
        "fold_count": 3,
        "oos_signals": 120,
        "folds": [{"test_samples": 40, "training_samples": 199}] * 3,
        "oos_predictions": _successful_prediction_rows(),
    }
    result = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=_manifest(0),
        dataset_sha256="d" * 64,
    )

    assert result["approved"] is False
    assert any(reason["code"] == "INSUFFICIENT_TRAINING_SAMPLES" for reason in result["rejection_reasons"])


def test_train_validate_exports_no_model_when_any_approval_gate_fails():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
        "samples": _samples(260),
        "provenance": {
            "cohort_selection_as_of_utc": "2026-11-15T00:00:00+00:00",
            "data_start_utc": "2026-11-02T00:00:00+00:00",
            "data_end_utc": "2026-11-15T00:00:00+00:00",
        },
    }
    manifest = _manifest(0)
    result = train_validate_group(
        group=group,
        manifest=manifest,
        dataset_sha256=hashlib.sha256(b"dataset").hexdigest(),
        policy={
            "min_training_samples_per_fold": 200,
            "min_walk_forward_folds": 3,
            "max_walk_forward_folds": 3,
            "min_oos_samples_per_fold": 20,
            "min_oos_samples": 1,
            "min_oos_signals": 1,
            "min_top_tercile_samples": 1,
            "bootstrap_resamples": 500,
        },
    )

    assert result["model"] is None
    assert result["approval"]["model_status"] == "DISABLED"
    assert any(
        reason["code"] == "COHORT_SELECTION_LOOKAHEAD"
        for reason in result["approval"]["rejection_reasons"]
    )



def test_train_validate_rejects_samples_outside_declared_manifest_window():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
        "samples": _samples(260),
        "provenance": {
            "cohort_selection_as_of_utc": "2026-11-01T00:00:00+00:00",
            "data_start_utc": "2026-11-02T00:00:00+00:00",
            "data_end_utc": "2026-11-15T00:00:00+00:00",
        },
    }
    result = train_validate_group(
        group=group,
        manifest=_manifest(0),
        dataset_sha256=hashlib.sha256(b"dataset").hexdigest(),
        policy={
            "min_training_samples_per_fold": 200,
            "min_walk_forward_folds": 3,
            "max_walk_forward_folds": 3,
            "min_oos_samples_per_fold": 20,
            "min_oos_samples": 1,
            "min_oos_signals": 1,
            "min_top_tercile_samples": 1,
            "bootstrap_resamples": 500,
        },
    )

    assert result["model"] is None
    assert any(
        reason["code"] == "SAMPLE_OUTSIDE_MANIFEST_DATA_WINDOW"
        for reason in result["approval"]["rejection_reasons"]
    )
