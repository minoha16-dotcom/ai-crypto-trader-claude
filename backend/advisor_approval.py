"""Reproducible, fail-closed approval gates for Advisor R-ranking models."""
import math
import random
import re
from datetime import datetime


MIN_TRAINING_SAMPLES_PER_GROUP = 200
MIN_VALID_WALK_FORWARD_FOLDS = 3
MIN_OOS_SIGNALS = 100
MIN_OOS_ELIGIBLE_SAMPLES = 100
MIN_OOS_SAMPLES_PER_FOLD = 20
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 731_991


def _finite(value, name):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _utc_timestamp(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return parsed


def validate_model_metadata(metadata):
    """Require every field needed to reproduce a fitted model."""
    reasons = []
    if not isinstance(metadata, dict):
        return ["MODEL_METADATA_MISSING"]
    required = (
        "model_version",
        "feature_schema_version",
        "algorithm",
        "hyperparameters",
        "backtest_config",
        "data_source",
        "dataset_sha256",
        "period_utc",
        "group_id",
    )
    for name in required:
        if metadata.get(name) in (None, "", {}):
            reasons.append(f"MODEL_METADATA_MISSING_{name.upper()}")

    digest = metadata.get("dataset_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        reasons.append("INVALID_DATASET_SHA256")

    period = metadata.get("period_utc")
    if isinstance(period, dict):
        try:
            start = _utc_timestamp(period.get("start"), "period_utc.start")
            end = _utc_timestamp(period.get("end"), "period_utc.end")
            if end <= start:
                reasons.append("INVALID_MODEL_UTC_PERIOD")
        except ValueError:
            reasons.append("INVALID_MODEL_UTC_PERIOD")
    else:
        reasons.append("INVALID_MODEL_UTC_PERIOD")

    source = metadata.get("data_source")
    if isinstance(source, dict):
        for name in ("provider", "exchange", "symbol", "market_type", "timeframe"):
            if not source.get(name):
                reasons.append(f"MODEL_DATA_SOURCE_MISSING_{name.upper()}")
    elif source:
        reasons.append("MODEL_DATA_SOURCE_MUST_BE_OBJECT")
    return reasons


def _mean(values):
    return sum(values) / len(values) if values else None


def _top_tercile_uplift(rows):
    ordered = sorted(
        rows,
        key=lambda row: _finite(row["predicted_r_multiple"], "predicted_r_multiple"),
        reverse=True,
    )
    top_count = max(1, math.ceil(len(ordered) / 3.0))
    all_mean = _mean([
        _finite(row["r_multiple"], "r_multiple") for row in rows
    ])
    top_mean = _mean([
        _finite(row["r_multiple"], "r_multiple") for row in ordered[:top_count]
    ])
    return top_mean, all_mean, top_mean - all_mean, top_count


def moving_block_bootstrap_uplift_ci(
    rows,
    *,
    replicates=BOOTSTRAP_REPLICATES,
    seed=BOOTSTRAP_SEED,
):
    """Paired circular moving-block bootstrap CI for top-tercile uplift.

    Rows must be chronological. Contiguous blocks preserve short-range temporal
    dependence better than independently resampling individual trade outcomes.
    """
    if len(rows) < 2:
        return None
    chronological = sorted(
        rows,
        key=lambda row: _finite(row["signal_timestamp_ms"], "signal_timestamp_ms"),
    )
    count = len(chronological)
    block_length = max(2, int(round(count ** (1.0 / 3.0))))
    rng = random.Random(seed)
    uplifts = []
    for _ in range(int(replicates)):
        indices = []
        while len(indices) < count:
            start = rng.randrange(count)
            indices.extend((start + offset) % count for offset in range(block_length))
        sample = [chronological[index] for index in indices[:count]]
        _, _, uplift, _ = _top_tercile_uplift(sample)
        uplifts.append(uplift)
    uplifts.sort()
    lower_index = max(0, min(len(uplifts) - 1, int(math.floor(0.025 * len(uplifts)))))
    upper_index = max(0, min(len(uplifts) - 1, int(math.ceil(0.975 * len(uplifts)) - 1)))
    return [round(uplifts[lower_index], 8), round(uplifts[upper_index], 8)]


def evaluate_ranking_approval(
    *,
    metadata,
    fold_records,
    oos_rows,
    min_training_samples=MIN_TRAINING_SAMPLES_PER_GROUP,
    min_valid_folds=MIN_VALID_WALK_FORWARD_FOLDS,
    min_oos_signals=MIN_OOS_SIGNALS,
    min_oos_eligible_samples=MIN_OOS_ELIGIBLE_SAMPLES,
    min_oos_samples_per_fold=MIN_OOS_SAMPLES_PER_FOLD,
    bootstrap_replicates=BOOTSTRAP_REPLICATES,
    bootstrap_seed=BOOTSTRAP_SEED,
):
    """Evaluate one independent Exchange × Symbol × Market × Timeframe group."""
    reasons = validate_model_metadata(metadata)
    group_id = metadata.get("group_id") if isinstance(metadata, dict) else None
    fold_records = list(fold_records or [])
    oos_rows = list(oos_rows or [])

    if not isinstance(group_id, str) or not group_id.strip():
        reasons.append("MODEL_GROUP_ID_MISSING")
    if not fold_records:
        reasons.append("NO_WALK_FORWARD_FOLDS")

    fold_ids = [str(fold.get("fold_id")) for fold in fold_records]
    if len(set(fold_ids)) != len(fold_ids):
        reasons.append("DUPLICATE_WALK_FORWARD_FOLD_ID")

    ordered_folds = sorted(
        fold_records,
        key=lambda fold: int(fold.get("test_start_ms", 0)),
    )
    previous_test_end = None
    for fold in ordered_folds:
        try:
            test_start = int(fold["test_start_ms"])
            test_end = int(fold["test_end_ms"])
            train_end = int(fold["train_end_ms"])
            if not (train_end <= test_start < test_end):
                reasons.append("INVALID_WALK_FORWARD_TEMPORAL_ORDER")
            if previous_test_end is not None and test_start < previous_test_end:
                reasons.append("OVERLAPPING_OOS_TEST_FOLDS")
            previous_test_end = test_end
        except (KeyError, TypeError, ValueError, OverflowError):
            # The fold-level sample gates still run, but missing temporal
            # boundaries prevent a fully reproducible approval.
            reasons.append("MISSING_WALK_FORWARD_TEMPORAL_METADATA")

    sample_ids = set()
    for row in oos_rows:
        if row.get("group_id") != group_id:
            reasons.append("MIXED_OR_MISSING_OOS_GROUP")
            break
        sample_id = row.get("sample_id")
        if not sample_id or sample_id in sample_ids:
            reasons.append("MISSING_OR_DUPLICATE_OOS_SAMPLE_ID")
            break
        sample_ids.add(sample_id)

    valid_folds = [
        fold for fold in fold_records
        if int(fold.get("training_eligible_samples", 0)) >= min_training_samples
        and int(fold.get("test_eligible_samples", 0)) >= min_oos_samples_per_fold
        and int(fold.get("oos_signals", 0)) >= min_oos_samples_per_fold
        and bool(fold.get("model_fit_success", False))
        and fold.get("test_start_ms") is not None
        and fold.get("test_end_ms") is not None
        and fold.get("train_end_ms") is not None
    ]
    if len(valid_folds) < min_valid_folds:
        reasons.append(f"VALID_WALK_FORWARD_FOLDS_BELOW_{min_valid_folds}")

    valid_fold_ids = {str(fold.get("fold_id")) for fold in valid_folds}
    scored_rows = [
        row for row in oos_rows
        if str(row.get("fold_id")) in valid_fold_ids
    ]
    oos_signal_count = sum(int(fold.get("oos_signals", 0)) for fold in valid_folds)
    eligible_count = len(scored_rows)
    training_counts = {
        str(fold.get("fold_id")): int(fold.get("training_eligible_samples", 0))
        for fold in fold_records
    }
    minimum_training_samples = min(
        (training_counts[str(fold.get("fold_id"))] for fold in valid_folds),
        default=0,
    )
    maximum_training_samples = max(training_counts.values(), default=0)

    if maximum_training_samples < min_training_samples:
        reasons.append(f"TRAINING_SAMPLES_BELOW_{min_training_samples}_FOR_GROUP")
    if oos_signal_count < min_oos_signals:
        reasons.append(f"OOS_SIGNALS_BELOW_{min_oos_signals}")
    if eligible_count < min_oos_eligible_samples:
        reasons.append(f"OOS_ELIGIBLE_SAMPLES_BELOW_{min_oos_eligible_samples}")

    top_mean = all_mean = uplift = None
    uplift_ci = None
    top_count = 0
    if eligible_count >= 2:
        try:
            top_mean, all_mean, uplift, top_count = _top_tercile_uplift(scored_rows)
            uplift_ci = moving_block_bootstrap_uplift_ci(
                scored_rows,
                replicates=bootstrap_replicates,
                seed=bootstrap_seed,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            reasons.append("INVALID_OOS_PREDICTIONS_OR_R_LABELS")
            top_mean = all_mean = uplift = uplift_ci = None

    if uplift is None or uplift <= 0:
        reasons.append("TOP_TERCILE_UPLIFT_NOT_POSITIVE")
    if not uplift_ci or uplift_ci[0] <= 0:
        reasons.append("UPLIFT_95_CI_LOWER_BOUND_NOT_ABOVE_ZERO")

    reasons = list(dict.fromkeys(reasons))
    approved = not reasons
    return {
        "model_status": "APPROVED" if approved else "DISABLED",
        "approval_status": "APPROVED" if approved else "REJECTED",
        "approval_reasons": reasons,
        "group_id": group_id,
        "training_eligible_samples_by_fold": training_counts,
        "maximum_training_eligible_samples": maximum_training_samples,
        "minimum_training_samples_across_valid_folds": minimum_training_samples,
        "required_training_samples_per_group": min_training_samples,
        "walk_forward_fold_count": len(fold_records),
        "valid_walk_forward_folds": len(valid_folds),
        "required_valid_walk_forward_folds": min_valid_folds,
        "oos_signals": oos_signal_count,
        "required_oos_signals": min_oos_signals,
        "oos_eligible_samples": eligible_count,
        "required_oos_eligible_samples": min_oos_eligible_samples,
        "top_tercile_sample_count": top_count,
        "top_tercile_mean_r": top_mean,
        "all_oos_mean_r": all_mean,
        "uplift": uplift,
        "uplift_confidence_interval_95": uplift_ci,
        "bootstrap": {
            "method": "circular_moving_block_bootstrap",
            "replicates": int(bootstrap_replicates),
            "seed": int(bootstrap_seed),
            "block_length": max(2, int(round(eligible_count ** (1.0 / 3.0))) if eligible_count else 0),
            "confidence_level": 0.95,
            "approval_rule": "lower_95_percentile_bound_must_be_greater_than_zero",
        },
        "valid_fold_details": valid_folds,
        "model_metadata": metadata,
    }
