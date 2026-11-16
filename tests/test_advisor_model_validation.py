import hashlib
import math
from datetime import datetime, timezone

import pytest

from backend.advisor_model_validation import (
    bootstrap_uplift_interval,
    evaluate_approval,
    fit_ridge,
    predict_ridge,
    train_validate_group,
    walk_forward_predict,
)


def _timestamp_ms(day, hour=0):
    return int(datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def _features(index):
    return {
        "trend_composite": 0.8 if index % 2 else -0.6,
        "rsi14": 60.0 if index % 2 else 38.0,
        "adx14": 30.0 + index % 10,
        "volume_ratio": 1.2 + (index % 5) * 0.1,
        "atr_pct": 0.5 + (index % 7) * 0.05,
    }


def _samples(count, start_ms=None):
    if start_ms is None:
        start_ms = _timestamp_ms(2)
    rows = []
    for index in range(count):
        signal_ms = start_ms + index * 60 * 60 * 1000
        r_value = 0.65 if index % 2 else -0.15
        rows.append({
            "sample_id": f"trade-{index}",
            "signal_timestamp": signal_ms,
            "exit_timestamp": signal_ms + 10 * 60 * 1000,
            "exit_reason": "TARGET" if r_value > 0 else "STOP",
            "features": _features(index),
            "r_multiple": r_value,
        })
    return rows


def _manifest(_unused=0):
    return {
        "source_provider": "Hyperliquid public leaderboard + public Info API",
        "source_url": "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
        "source_metadata_url": "https://underhyper.com/data/leaderboard.json",
        "source_license": (
            "Hyperliquid raw public data; endpoint does not state a separate CC BY grant. "
            "UnderHyper metadata is CC BY 4.0 and is not assumed to license raw Hyperliquid data."
        ),
        "source_snapshot_utc": "2026-11-01T00:00:00+00:00",
        "source_snapshot_sha256": "a" * 64,
        "source_metadata_sha256": "b" * 64,
        "cohort_selection_as_of_utc": "2026-11-01T00:00:00+00:00",
        "data_start_utc": "2026-11-02T00:00:00+00:00",
        "data_end_utc": "2026-12-15T00:00:00+00:00",
        "model_version": "1.0.0",
        "feature_schema_version": "advisor-r5-v1",
        "code_sha": "abcdef1234567",
        "backtest_config": {
            "risk_unit_definition": "net PnL divided by standardized max(2xATR, 0.5pct entry) risk",
            "fee_model": "actual fill fees plus configured residual fees",
            "slippage_model": "conservative 2 bps each side",
            "slippage_bps": 2.0,
            "fee_rate": 0.0006,
            "funding_included": True,
            "entry_rule": "first observed public open fill",
            "exit_rule": "first full close of the reconstructed position",
        },
    }


def _successful_prediction_rows(actual_zero=False):
    rows = []
    base = _timestamp_ms(3)
    for fold in range(1, 4):
        fold_start = base + (fold - 1) * 48 * 60 * 60 * 1000
        for index in range(40):
            predicted = float(40 - index)
            actual = 0.0 if actual_zero else (1.0 if index < 14 else -0.25)
            signal_ms = fold_start + index * 60 * 60 * 1000
            rows.append({
                "sample_id": f"f{fold}-{index}",
                "fold_id": fold,
                "signal_timestamp": datetime.fromtimestamp(
                    signal_ms / 1000, tz=timezone.utc
                ).isoformat(),
                "exit_timestamp": datetime.fromtimestamp(
                    (signal_ms + 10 * 60 * 1000) / 1000, tz=timezone.utc
                ).isoformat(),
                "exit_reason": "TARGET" if actual > 0 else "STOP",
                "predicted_r_multiple": predicted,
                "r_multiple": actual,
            })
    return rows


def _approval_fold_reports(sample_count=40, train_count=250, excluded_count=0, excluded_reason=None):
    base = _timestamp_ms(3)
    folds = []
    for fold in range(1, 4):
        start_ms = base + (fold - 1) * 48 * 60 * 60 * 1000
        end_ms = start_ms + 39 * 60 * 60 * 1000
        folds.append({
            "fold_id": fold,
            "training_samples": train_count,
            "test_signal_count": 40,
            "test_samples": sample_count,
            "excluded_test_samples": excluded_count,
            "excluded_test_reasons": (
                {excluded_reason: excluded_count} if excluded_reason else {}
            ),
            "training_label_end_utc": datetime.fromtimestamp(
                (start_ms - 60 * 1000) / 1000, tz=timezone.utc
            ).isoformat(),
            "test_start_utc": datetime.fromtimestamp(
                start_ms / 1000, tz=timezone.utc
            ).isoformat(),
            "test_end_utc": datetime.fromtimestamp(
                end_ms / 1000, tz=timezone.utc
            ).isoformat(),
        })
    return folds


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
    first_test_start = samples[200]["signal_timestamp"]
    for row in samples[120:200]:
        row["exit_timestamp"] = first_test_start + 1

    with pytest.raises(ValueError, match="fully matured eligible training labels"):
        walk_forward_predict(
            samples,
            min_training_samples=200,
            min_folds=3,
            max_folds=3,
            min_test_samples_per_fold=20,
            min_test_signals_per_fold=20,
        )


def test_walk_forward_produces_three_chronological_folds_and_separate_oos_counts():
    result = walk_forward_predict(
        _samples(320),
        min_training_samples=200,
        min_folds=3,
        max_folds=3,
        min_test_samples_per_fold=20,
        min_test_signals_per_fold=20,
    )

    assert result["fold_count"] == 3
    assert result["min_training_samples_per_fold"] >= 200
    assert len(result["oos_predictions"]) == 120
    assert result["oos_signals"] == 120
    assert result["oos_samples"] == 120
    assert result["folds"][0]["test_end_utc"] < result["folds"][1]["test_start_utc"]
    assert result["folds"][1]["test_end_utc"] < result["folds"][2]["test_start_utc"]


def test_period_end_and_liquidation_are_excluded_but_remain_oos_signals():
    samples = _samples(320)
    for index in range(200, 320, 10):
        samples[index]["exit_reason"] = "PERIOD_END" if index % 20 == 0 else "LIQUIDATION_GAP"

    result = walk_forward_predict(
        samples,
        min_training_samples=200,
        min_folds=3,
        max_folds=3,
        min_test_samples_per_fold=20,
        min_test_signals_per_fold=20,
    )

    assert result["oos_signals"] == 120
    assert result["oos_samples"] == 108
    assert result["input_excluded_samples"] == 12
    assert result["input_excluded_reasons"] == {
        "LIQUIDATION_GAP": 6,
        "PERIOD_END": 6,
    }
    assert result["oos_excluded_samples"] == 12
    assert result["oos_excluded_reasons"] == {
        "LIQUIDATION_GAP": 6,
        "PERIOD_END": 6,
    }
    assert all(fold["test_signal_count"] == 40 for fold in result["folds"])
    assert all(fold["test_samples"] == 36 for fold in result["folds"])


def test_unknown_exit_reason_is_never_a_training_label():
    samples = _samples(260)
    samples[200]["exit_reason"] = "MANUAL_CLOSE"
    result = walk_forward_predict(
        samples,
        min_training_samples=200,
        min_folds=3,
        max_folds=3,
        min_test_samples_per_fold=20,
        min_test_signals_per_fold=20,
    )
    assert result["input_excluded_reasons"]["MANUAL_CLOSE"] == 1
    assert result["input_eligible_samples"] == 259
    assert result["oos_signals"] == 60
    assert result["oos_samples"] == 59
    assert result["oos_excluded_samples"] == 1
    assert result["oos_excluded_reasons"] == {"MANUAL_CLOSE": 1}


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
    }
    rows = _successful_prediction_rows()
    walk_forward = {
        "min_training_samples_per_fold": 250,
        "fold_count": 3,
        "oos_signals": 120,
        "oos_samples": 120,
        "input_excluded_samples": 9,
        "input_excluded_reasons": {"OUTSIDE_OOS_WINDOW": 9},
        "oos_excluded_samples": 0,
        "oos_excluded_reasons": {},
        "folds": _approval_fold_reports(),
        "oos_predictions": rows,
    }
    manifest = _manifest()

    approved = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=manifest,
        dataset_sha256="c" * 64,
    )
    assert approved["approved"] is True
    assert approved["model_status"] == "APPROVED"
    assert approved["metrics"]["uplift"] > 0
    assert approved["metrics"]["uplift_confidence_interval_95"][0] > 0
    assert approved["metrics"]["top_tercile_samples"] >= 30
    assert approved["metrics"]["excluded_samples"] == 0
    assert approved["metrics"]["input_excluded_samples"] == 9

    leaking_manifest = dict(manifest)
    leaking_manifest["cohort_selection_as_of_utc"] = "2026-11-10T00:00:00+00:00"
    rejected = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=leaking_manifest,
        dataset_sha256="c" * 64,
    )
    assert rejected["approved"] is False
    assert rejected["model_status"] == "DISABLED"
    assert any(
        reason["code"] == "COHORT_SELECTION_LOOKAHEAD"
        for reason in rejected["rejection_reasons"]
    )


def test_approval_rejects_top_tercile_without_positive_lower_confidence_bound():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
    }
    rows = _successful_prediction_rows(actual_zero=True)
    walk_forward = {
        "min_training_samples_per_fold": 250,
        "fold_count": 3,
        "oos_signals": 120,
        "oos_samples": 120,
        "input_excluded_samples": 0,
        "input_excluded_reasons": {},
        "oos_excluded_samples": 0,
        "oos_excluded_reasons": {},
        "folds": _approval_fold_reports(),
        "oos_predictions": rows,
    }
    result = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=_manifest(),
        dataset_sha256="d" * 64,
        policy={"bootstrap_resamples": 500},
    )

    assert result["approved"] is False
    assert any(
        reason["code"] == "UPLIFT_CI_INCLUDES_ZERO"
        for reason in result["rejection_reasons"]
    )


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
        "oos_samples": 120,
        "input_excluded_samples": 0,
        "input_excluded_reasons": {},
        "oos_excluded_samples": 0,
        "oos_excluded_reasons": {},
        "folds": _approval_fold_reports(train_count=199),
        "oos_predictions": _successful_prediction_rows(),
    }
    result = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=_manifest(),
        dataset_sha256="e" * 64,
    )

    assert result["approved"] is False
    assert any(
        reason["code"] == "INSUFFICIENT_TRAINING_SAMPLES"
        for reason in result["rejection_reasons"]
    )


def test_train_validate_exports_no_model_when_cohort_selection_looks_ahead():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
        "samples": _samples(260),
        "provenance": {
            "cohort_selection_as_of_utc": "2026-11-15T00:00:00+00:00",
            "data_start_utc": "2026-11-02T00:00:00+00:00",
            "data_end_utc": "2026-12-15T00:00:00+00:00",
        },
    }
    result = train_validate_group(
        group=group,
        manifest=_manifest(),
        dataset_sha256=hashlib.sha256(b"dataset").hexdigest(),
        policy={
            "min_training_samples_per_fold": 200,
            "min_walk_forward_folds": 3,
            "max_walk_forward_folds": 3,
            "min_oos_samples_per_fold": 20,
            "min_oos_signals_per_fold": 20,
            "min_oos_samples": 1,
            "min_oos_signals": 1,
            "min_top_tercile_samples": 1,
            "bootstrap_resamples": 500,
        },
    )

    assert result["model"] is None
    assert result["approval"]["model_status"] == "DISABLED"
    assert any(
        reason["code"] == "SAMPLE_PRECEDES_COHORT_CUTOFF"
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
            "data_end_utc": "2026-11-05T00:00:00+00:00",
        },
    }
    result = train_validate_group(
        group=group,
        manifest=_manifest(),
        dataset_sha256=hashlib.sha256(b"dataset").hexdigest(),
        policy={
            "min_training_samples_per_fold": 200,
            "min_walk_forward_folds": 3,
            "max_walk_forward_folds": 3,
            "min_oos_samples_per_fold": 20,
            "min_oos_signals_per_fold": 20,
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


def test_end_to_end_model_exports_only_when_all_provenance_and_oos_gates_pass():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
        "samples": _samples(500),
    }
    result = train_validate_group(
        group=group,
        manifest=_manifest(),
        dataset_sha256=hashlib.sha256(b"frozen-dataset").hexdigest(),
    )

    assert result["approval"]["approved"] is True
    assert result["model"] is not None
    assert result["approval"]["metrics"]["oos_signals"] >= 100
    assert result["approval"]["metrics"]["oos_samples"] >= 100
    assert result["approval"]["metrics"]["uplift_confidence_interval_95"][0] > 0



def test_approval_reports_oos_exclusions_separately_from_full_dataset_exclusions():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
    }
    predictions = _successful_prediction_rows()
    folds = _approval_fold_reports(
        sample_count=39,
        excluded_count=1,
        excluded_reason="MANUAL_CLOSE",
    )
    walk_forward = {
        "min_training_samples_per_fold": 250,
        "fold_count": 3,
        "oos_signals": 120,
        "oos_samples": 117,
        "oos_excluded_samples": 3,
        "oos_excluded_reasons": {"MANUAL_CLOSE": 3},
        "input_excluded_samples": 20,
        "input_excluded_reasons": {"PERIOD_END": 10, "UNKNOWN": 10},
        "folds": folds,
        "oos_predictions": predictions[:39] + predictions[40:79] + predictions[80:119],
    }

    result = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=_manifest(),
        dataset_sha256="f" * 64,
        policy={"bootstrap_resamples": 500},
    )

    assert result["metrics"]["oos_signals"] == 120
    assert result["metrics"]["oos_samples"] == 117
    assert result["metrics"]["eligible_samples"] == 117
    assert result["metrics"]["excluded_samples"] == 3
    assert result["metrics"]["excluded_sample_reasons"] == {"MANUAL_CLOSE": 3}
    assert result["metrics"]["input_excluded_samples"] == 20



def test_bootstrap_rejects_oos_samples_concentrated_in_too_few_time_blocks():
    rows = _successful_prediction_rows()
    for row in rows:
        row["signal_timestamp"] = "2026-11-03T00:00:00+00:00"
        row["exit_timestamp"] = "2026-11-03T00:10:00+00:00"

    assert bootstrap_uplift_interval(rows, resamples=500, seed=7) is None


def test_approval_disables_model_when_oos_signals_are_too_time_clustered():
    group = {
        "exchange": "hyperliquid",
        "symbol": "BTC/USDC",
        "market_type": "futures",
        "timeframe": "1h",
    }
    rows = _successful_prediction_rows()
    for row in rows:
        fold_id = row["fold_id"]
        fold_start = _approval_fold_reports()[fold_id - 1]["test_start_utc"]
        row["signal_timestamp"] = fold_start
        signal_ms = int(datetime.fromisoformat(fold_start).timestamp() * 1000)
        row["exit_timestamp"] = datetime.fromtimestamp(
            (signal_ms + 10 * 60 * 1000) / 1000,
            tz=timezone.utc,
        ).isoformat()
    walk_forward = {
        "min_training_samples_per_fold": 250,
        "fold_count": 3,
        "oos_signals": 120,
        "oos_samples": 120,
        "oos_excluded_samples": 0,
        "oos_excluded_reasons": {},
        "input_excluded_samples": 0,
        "input_excluded_reasons": {},
        "folds": _approval_fold_reports(),
        "oos_predictions": rows,
    }

    result = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=_manifest(),
        dataset_sha256="9" * 64,
        policy={"bootstrap_resamples": 500},
    )

    assert result["approved"] is False
    assert result["metrics"]["oos_time_blocks_per_fold"] == [1, 1, 1]
    assert any(
        reason["code"] == "INSUFFICIENT_OOS_TIME_BLOCKS"
        for reason in result["rejection_reasons"]
    )
