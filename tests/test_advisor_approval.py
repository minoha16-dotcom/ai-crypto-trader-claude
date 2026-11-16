import pytest

from backend.advisor_approval import evaluate_ranking_approval, validate_model_metadata


def metadata(group_id="okx:BTC-USDT-SWAP:SWAP:1h"):
    return {
        "model_version": "advisor-ridge-r-1.0.0",
        "feature_schema_version": "advisor-r-features-1",
        "algorithm": "ridge_regression_l2",
        "hyperparameters": {"alpha": 2.0},
        "backtest_config": {"fee_rate": 0.0006, "slippage_bps": 2.0},
        "data_source": {
            "provider": "ccxt_exchange_registry",
            "exchange": "okx",
            "symbol": "BTC/USDT:USDT",
            "market_type": "futures",
            "timeframe": "1h",
        },
        "dataset_sha256": "a" * 64,
        "period_utc": {
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-06-01T00:00:00+00:00",
        },
        "group_id": group_id,
    }


def approved_fixture():
    group_id = "okx:BTC-USDT-SWAP:SWAP:1h"
    folds = []
    rows = []
    for fold_number in range(3):
        fold_id = f"{group_id}:fold:{fold_number + 1}"
        folds.append({
            "fold_id": fold_id,
            "training_eligible_samples": 250,
            "test_eligible_samples": 100,
            "oos_signals": 100,
            "model_fit_success": True,
            "train_end_ms": 1_000_000 + fold_number * 1_000_000,
            "test_start_ms": 1_100_000 + fold_number * 1_000_000,
            "test_end_ms": 1_900_000 + fold_number * 1_000_000,
        })
        for index in range(100):
            # The top predicted third has materially higher realized R.
            band = index // 100
            predicted = float(band)
            realized = (-5.0, 0.0, 5.0)[band]
            rows.append({
                "group_id": group_id,
                "fold_id": fold_id,
                "sample_id": f"{fold_id}:{index}",
                "signal_timestamp_ms": 1_100_001 + fold_number * 1_000_000 + index,
                "predicted_r_multiple": predicted,
                "r_multiple": realized,
            })
    return folds, rows


def test_model_approval_requires_all_gates_and_returns_reproducible_metrics():
    folds, rows = approved_fixture()
    result = evaluate_ranking_approval(
        metadata=metadata(),
        fold_records=folds,
        oos_rows=rows,
        bootstrap_replicates=300,
    )
    assert result["approval_status"] == "APPROVED"
    assert result["model_status"] == "APPROVED"
    assert result["valid_walk_forward_folds"] == 3
    assert result["maximum_training_eligible_samples"] == 250
    assert result["oos_signals"] == 300
    assert result["oos_eligible_samples"] == 300
    assert result["uplift"] > 0
    assert result["uplift_confidence_interval_95"][0] > 0
    assert result["approval_reasons"] == []


@pytest.mark.parametrize(
    "mutator, expected_reason",
    [
        (lambda f, r: f.__setitem__(0, {**f[0], "training_eligible_samples": 199}), "VALID_WALK_FORWARD_FOLDS_BELOW_3"),
        (lambda f, r: f.pop(), "VALID_WALK_FORWARD_FOLDS_BELOW_3"),
        (lambda f, r: [r.pop() for _ in range(250)], "OOS_ELIGIBLE_SAMPLES_BELOW_100"),
    ],
)
def test_model_approval_disables_when_sample_or_fold_gates_fail(mutator, expected_reason):
    folds, rows = approved_fixture()
    mutator(folds, rows)
    result = evaluate_ranking_approval(
        metadata=metadata(),
        fold_records=folds,
        oos_rows=rows,
        bootstrap_replicates=100,
    )
    assert result["model_status"] == "DISABLED"
    assert expected_reason in result["approval_reasons"]


def test_model_approval_rejects_mixed_group_rows():
    folds, rows = approved_fixture()
    rows[0]["group_id"] = "another:market:group"
    result = evaluate_ranking_approval(
        metadata=metadata(),
        fold_records=folds,
        oos_rows=rows,
        bootstrap_replicates=100,
    )
    assert result["model_status"] == "DISABLED"
    assert "MIXED_OR_MISSING_OOS_GROUP" in result["approval_reasons"]


def test_model_metadata_requires_dataset_hash_and_utc_period():
    broken = metadata()
    broken["dataset_sha256"] = "wrong"
    broken["period_utc"] = {"start": "2026-01-01", "end": "2026-02-01"}
    reasons = validate_model_metadata(broken)
    assert "INVALID_DATASET_SHA256" in reasons
    assert "INVALID_MODEL_UTC_PERIOD" in reasons
