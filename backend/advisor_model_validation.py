"""Deterministic, per-market Advisor model validation and approval.

The approval gate is deliberately fail-closed. It trains only on outcomes that
were closed before each chronological test window and exports a final model
only after the pre-registered sample, fold, uplift and provenance checks pass.
"""
from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left
from datetime import datetime, timezone
import hashlib
import math
import random
import re
from typing import Any


MODEL_VERSION = "1.2.0"
FEATURE_SCHEMA_VERSION = "advisor-r5-v3"
MODEL_FAMILY = "ridge_linear_r"
FEATURE_KEYS = (
    "trend_composite",
    "rsi14",
    "adx14",
    "volume_ratio",
    "atr_pct",
)
DEFAULT_POLICY = {
    "min_training_samples_per_fold": 200,
    "min_walk_forward_folds": 3,
    "max_walk_forward_folds": 3,
    "min_oos_signals": 100,
    "min_oos_samples": 100,
    "min_oos_samples_per_fold": 20,
    "min_oos_signals_per_fold": 20,
    "min_oos_time_blocks_per_fold": 5,
    "bootstrap_block_hours": 24,
    "min_top_tercile_samples": 30,
    "min_preparation_coverage_pct": 80.0,
    "bootstrap_resamples": 2000,
    "confidence_level": 0.95,
    "bootstrap_seed": 271828,
    "ridge_alpha": 1.0,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _timestamp_ms(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a UTC timestamp")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field_name} must be finite")
        # Unix timestamps in this dataset are accepted in milliseconds only.
        if number < 100_000_000_000:
            raise ValueError(f"{field_name} must be Unix milliseconds or ISO-8601 UTC")
        return int(number)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone; UTC is required")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _finite_number(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _feature_vector(features: dict[str, Any]) -> list[float]:
    if not isinstance(features, dict):
        raise ValueError("features must be an object")
    missing = [key for key in FEATURE_KEYS if key not in features]
    if missing:
        raise ValueError("missing feature(s): " + ", ".join(missing))

    trend = max(-1.0, min(1.0, _finite_number(features["trend_composite"], "trend_composite")))
    rsi = max(0.0, min(100.0, _finite_number(features["rsi14"], "rsi14")))
    adx = max(0.0, min(100.0, _finite_number(features["adx14"], "adx14")))
    volume_ratio = max(0.0, _finite_number(features["volume_ratio"], "volume_ratio"))
    atr_pct = max(0.0, _finite_number(features["atr_pct"], "atr_pct"))

    # In schema r5-v2, trend_composite and RSI14 are position-relative:
    # positive values consistently indicate conditions supportive of LONG or
    # SHORT positions. EMA alignment and price-vs-EMA20 are merged upstream
    # into one trend_composite feature.
    return [
        trend,
        (rsi - 50.0) / 50.0,
        adx / 50.0,
        min(2.0, math.log1p(volume_ratio) / math.log(6.0)),
        min(2.0, math.log1p(atr_pct) / math.log1p(3.0)),
    ]


def _gaussian_solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense linear system using pivoted Gauss-Jordan elimination."""
    size = len(vector)
    augmented = [
        [float(value) for value in matrix[row]] + [float(vector[row])]
        for row in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("ridge normal equation is singular")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            multiplier = augmented[row][column]
            if multiplier:
                augmented[row] = [
                    left - multiplier * right
                    for left, right in zip(augmented[row], augmented[column])
                ]
    return [augmented[row][-1] for row in range(size)]


def fit_ridge(samples: list[dict[str, Any]], alpha: float = 1.0) -> dict[str, Any]:
    """Fit a small L2-regularized linear model to net R-multiple labels."""
    if len(samples) < 2:
        raise ValueError("at least two training samples are required")
    alpha = _finite_number(alpha, "ridge_alpha")
    if alpha <= 0:
        raise ValueError("ridge_alpha must be positive")

    rows = []
    targets = []
    for sample in samples:
        rows.append([1.0] + _feature_vector(sample["features"]))
        targets.append(_finite_number(sample["r_multiple"], "r_multiple"))

    width = len(FEATURE_KEYS) + 1
    gram = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for row, target in zip(rows, targets):
        for i in range(width):
            rhs[i] += row[i] * target
            for j in range(width):
                gram[i][j] += row[i] * row[j]

    # Do not regularize the intercept.
    for index in range(1, width):
        gram[index][index] += alpha

    solution = _gaussian_solve(gram, rhs)
    return {
        "model_family": MODEL_FAMILY,
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_keys": list(FEATURE_KEYS),
        "ridge_alpha": alpha,
        "intercept": solution[0],
        "coefficients": {
            key: solution[index + 1] for index, key in enumerate(FEATURE_KEYS)
        },
        "training_samples": len(samples),
    }


def predict_ridge(model: dict[str, Any], features: dict[str, Any]) -> float:
    vector = _feature_vector(features)
    coefficients = model.get("coefficients") or {}
    result = _finite_number(model.get("intercept"), "model.intercept")
    for key, value in zip(FEATURE_KEYS, vector):
        result += _finite_number(coefficients.get(key), f"model.coefficients.{key}") * value
    if not math.isfinite(result):
        raise ValueError("model prediction is not finite")
    return result


ELIGIBLE_EXIT_REASONS = {
    "STOP", "STOP_GAP", "TARGET", "TARGET_GAP", "TIME_EXIT",
}


def _validate_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate all signals; keep disallowed exits for exclusion accounting."""
    if not isinstance(samples, list):
        raise ValueError("samples must be a list")
    validated = []
    seen_ids = set()
    for index, row in enumerate(samples):
        if not isinstance(row, dict):
            raise ValueError(f"sample {index} must be an object")
        signal_ms = _timestamp_ms(row.get("signal_timestamp"), "signal_timestamp")
        exit_ms = _timestamp_ms(row.get("exit_timestamp"), "exit_timestamp")
        if exit_ms < signal_ms:
            raise ValueError(f"sample {index} exits before its signal")
        feature_values = _feature_vector(row.get("features"))
        sample_id = str(row.get("sample_id") or f"sample-{index}")
        if sample_id in seen_ids:
            raise ValueError(f"duplicate sample_id: {sample_id}")
        seen_ids.add(sample_id)
        position_side = str(row.get("position_side") or "").strip().upper()
        if position_side not in {"LONG", "SHORT"}:
            raise ValueError(f"sample {index} must declare position_side as LONG or SHORT")
        exit_reason = str(row.get("exit_reason") or "UNKNOWN").strip().upper()
        eligible = exit_reason in ELIGIBLE_EXIT_REASONS
        r_multiple = _finite_number(row.get("r_multiple"), "r_multiple") if eligible else None
        validated.append({
            "sample_id": sample_id,
            "signal_timestamp_ms": signal_ms,
            "exit_timestamp_ms": exit_ms,
            "exit_reason": exit_reason,
            "position_side": position_side,
            "eligible": eligible,
            "r_multiple": r_multiple,
            "features": dict(row["features"]),
            "feature_vector": feature_values,
        })
    validated.sort(key=lambda row: (
        row["signal_timestamp_ms"], row["exit_timestamp_ms"], row["sample_id"]
    ))
    return validated
def walk_forward_predict(
    samples: list[dict[str, Any]],
    *,
    min_training_samples: int = 200,
    min_folds: int = 3,
    max_folds: int = 5,
    min_test_samples_per_fold: int = 20,
    min_test_signals_per_fold: int = 20,
    ridge_alpha: float = 1.0,
) -> dict[str, Any]:
    """Expanding chronological validation with timestamp groups kept intact.

    Only eligible exits train the model. A training label must be fully matured
    before the first signal in its test fold. All signals with the same UTC
    timestamp stay in one test fold, preventing concurrent-wallet leakage.
    """
    min_training_samples = int(min_training_samples)
    min_folds = int(min_folds)
    max_folds = int(max_folds)
    min_test_samples_per_fold = int(min_test_samples_per_fold)
    min_test_signals_per_fold = int(min_test_signals_per_fold)
    if min_training_samples < 2 or min_folds < 1 or max_folds < min_folds:
        raise ValueError("invalid walk-forward policy")
    if min_test_samples_per_fold < 1 or min_test_signals_per_fold < 1:
        raise ValueError("minimum test signals and samples must be positive")

    ordered = _validate_samples(samples)
    eligible_rows = [row for row in ordered if row["eligible"]]
    if len(eligible_rows) < min_training_samples:
        raise ValueError(
            f"insufficient eligible training labels: need {min_training_samples}, "
            f"received {len(eligible_rows)}"
        )

    # Candidate test starts are whole UTC signal-time groups. Do not begin OOS
    # until at least the required number of eligible labels have actually matured.
    time_groups: list[list[dict[str, Any]]] = []
    for row in ordered:
        if (
            not time_groups
            or time_groups[-1][0]["signal_timestamp_ms"] != row["signal_timestamp_ms"]
        ):
            time_groups.append([row])
        else:
            time_groups[-1].append(row)

    eligible_exit_times = sorted(
        row["exit_timestamp_ms"] for row in eligible_rows
    )
    first_test_group = None
    for index, time_group in enumerate(time_groups):
        candidate_start = time_group[0]["signal_timestamp_ms"]
        if bisect_left(eligible_exit_times, candidate_start) >= min_training_samples:
            first_test_group = index
            break
    if first_test_group is None:
        raise ValueError(
            f"insufficient fully matured eligible training labels: need "
            f"{min_training_samples} before an OOS signal"
        )

    test_groups = time_groups[first_test_group:]
    if not test_groups:
        raise ValueError("insufficient OOS signal groups")

    # Partition contiguous time groups into the largest possible number of
    # folds while satisfying both signal and eligible-outcome minima in each.
    # If one partition cannot satisfy every fold, retry with fewer folds.
    selected_chunks = None
    selected_fold_count = 0
    for candidate_fold_count in range(max_folds, min_folds - 1, -1):
        cursor = 0
        remaining_signals = sum(len(group) for group in test_groups)
        remaining_eligible = sum(
            1 for group in test_groups for row in group if row["eligible"]
        )
        chunks = []
        possible = True

        for fold_index in range(candidate_fold_count):
            folds_left = candidate_fold_count - fold_index
            max_end = len(test_groups) - (folds_left - 1)
            if folds_left == 1:
                chunk = test_groups[cursor:]
                signal_count = sum(len(group) for group in chunk)
                eligible_count = sum(
                    1 for group in chunk for row in group if row["eligible"]
                )
                if (
                    signal_count < min_test_signals_per_fold
                    or eligible_count < min_test_samples_per_fold
                ):
                    possible = False
                    break
                chunks.append(chunk)
                cursor = len(test_groups)
                break

            target_signals = math.ceil(remaining_signals / folds_left)
            target_eligible = math.ceil(remaining_eligible / folds_left)
            candidate_boundaries = []
            signal_count = 0
            eligible_count = 0
            for end_index in range(cursor + 1, max_end + 1):
                next_group = test_groups[end_index - 1]
                signal_count += len(next_group)
                eligible_count += sum(1 for row in next_group if row["eligible"])
                signals_left = remaining_signals - signal_count
                eligible_left = remaining_eligible - eligible_count
                if (
                    signal_count < min_test_signals_per_fold
                    or eligible_count < min_test_samples_per_fold
                    or signals_left < min_test_signals_per_fold * (folds_left - 1)
                    or eligible_left < min_test_samples_per_fold * (folds_left - 1)
                ):
                    continue
                score = (
                    abs(signal_count - target_signals) / max(1, remaining_signals)
                    + abs(eligible_count - target_eligible) / max(1, remaining_eligible)
                )
                candidate_boundaries.append((
                    score,
                    end_index,
                    signal_count,
                    eligible_count,
                ))

            if not candidate_boundaries:
                possible = False
                break
            _, chosen_end, chosen_signals, chosen_eligible = min(
                candidate_boundaries,
                key=lambda item: (item[0], item[1]),
            )
            chunks.append(test_groups[cursor:chosen_end])
            cursor = chosen_end
            remaining_signals -= chosen_signals
            remaining_eligible -= chosen_eligible

        if possible and cursor == len(test_groups) and len(chunks) == candidate_fold_count:
            selected_chunks = chunks
            selected_fold_count = candidate_fold_count
            break

    if selected_chunks is None:
        raise ValueError(
            f"insufficient chronological OOS windows: need {min_folds} folds "
            f"with at least {min_test_signals_per_fold} signals and "
            f"{min_test_samples_per_fold} eligible outcomes per fold"
        )

    predictions = []
    fold_reports = []
    training_counts = []
    for fold_id, chunk in enumerate(selected_chunks, start=1):
        test_rows = [row for time_group in chunk for row in time_group]
        test_start_ms = test_rows[0]["signal_timestamp_ms"]

        train_rows = [
            row for row in eligible_rows
            if row["exit_timestamp_ms"] < test_start_ms
        ]
        if len(train_rows) < min_training_samples:
            raise ValueError(
                f"fold {fold_id} has only {len(train_rows)} fully matured "
                f"eligible training labels; minimum is {min_training_samples}"
            )

        fitted_model = fit_ridge(train_rows, alpha=ridge_alpha)
        eligible_test_rows = [row for row in test_rows if row["eligible"]]
        excluded_test_rows = [row for row in test_rows if not row["eligible"]]
        excluded_reasons: dict[str, int] = {}
        for row in excluded_test_rows:
            excluded_reasons[row["exit_reason"]] = (
                excluded_reasons.get(row["exit_reason"], 0) + 1
            )

        fold_prediction_rows = []
        for row in eligible_test_rows:
            predicted = predict_ridge(fitted_model, row["features"])
            prediction = {
                "sample_id": row["sample_id"],
                "fold_id": fold_id,
                "signal_timestamp": _iso_utc(row["signal_timestamp_ms"]),
                "exit_timestamp": _iso_utc(row["exit_timestamp_ms"]),
                "exit_reason": row["exit_reason"],
                "predicted_r_multiple": predicted,
                "r_multiple": row["r_multiple"],
            }
            predictions.append(prediction)
            fold_prediction_rows.append(prediction)

        training_counts.append(len(train_rows))
        fold_reports.append({
            "fold_id": fold_id,
            "training_samples": len(train_rows),
            "test_signal_count": len(test_rows),
            "test_samples": len(eligible_test_rows),
            "oos_time_blocks": len({
                row["signal_timestamp_ms"] // (6 * 60 * 60 * 1000)
                for row in eligible_test_rows
            }),
            "excluded_test_samples": len(excluded_test_rows),
            "excluded_test_reasons": dict(sorted(excluded_reasons.items())),
            "training_signal_start_utc": _iso_utc(train_rows[0]["signal_timestamp_ms"]),
            "training_label_end_utc": _iso_utc(
                max(row["exit_timestamp_ms"] for row in train_rows)
            ),
            "test_start_utc": _iso_utc(test_rows[0]["signal_timestamp_ms"]),
            "test_end_utc": _iso_utc(test_rows[-1]["signal_timestamp_ms"]),
            "test_exit_end_utc": _iso_utc(
                max(row["exit_timestamp_ms"] for row in test_rows)
            ),
            "model_parameters": {
                "model_family": fitted_model["model_family"],
                "model_version": fitted_model["model_version"],
                "feature_schema_version": fitted_model["feature_schema_version"],
                "ridge_alpha": fitted_model["ridge_alpha"],
                "intercept": fitted_model["intercept"],
                "coefficients": fitted_model["coefficients"],
            },
            "test_predictions": fold_prediction_rows,
        })

    if len(fold_reports) < min_folds:
        raise ValueError(
            f"only {len(fold_reports)} valid walk-forward folds; minimum is {min_folds}"
        )

    all_excluded = [row for row in ordered if not row["eligible"]]
    excluded_reasons: dict[str, int] = {}
    for row in all_excluded:
        excluded_reasons[row["exit_reason"]] = (
            excluded_reasons.get(row["exit_reason"], 0) + 1
        )
    oos_excluded_reasons: dict[str, int] = {}
    for fold in fold_reports:
        for reason, count in (fold.get("excluded_test_reasons") or {}).items():
            oos_excluded_reasons[reason] = (
                oos_excluded_reasons.get(reason, 0) + int(count)
            )
    oos_excluded_samples = sum(oos_excluded_reasons.values())

    final_model = fit_ridge(eligible_rows, alpha=ridge_alpha)
    return {
        "fold_count": selected_fold_count,
        "min_training_samples_per_fold": min(training_counts),
        "total_training_rows_used_across_folds": sum(training_counts),
        "oos_signals": sum(row["test_signal_count"] for row in fold_reports),
        "oos_samples": len(predictions),
        "oos_excluded_samples": oos_excluded_samples,
        "oos_excluded_reasons": dict(sorted(oos_excluded_reasons.items())),
        "input_signals": len(ordered),
        "input_eligible_samples": len(eligible_rows),
        "input_excluded_samples": len(all_excluded),
        "input_excluded_reasons": dict(sorted(excluded_reasons.items())),
        "oos_predictions": predictions,
        "folds": fold_reports,
        "final_candidate_model": final_model,
    }
def _top_tercile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    count = max(1, math.ceil(len(rows) / 3.0))
    return sorted(
        rows,
        key=lambda row: (float(row["predicted_r_multiple"]), str(row["sample_id"])),
        reverse=True,
    )[:count]


def _uplift(rows: list[dict[str, Any]]) -> tuple[float, float, float, int]:
    by_fold: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_fold[int(row["fold_id"])].append(row)
    all_r = [float(row["r_multiple"]) for row in rows]
    top_rows = [
        row for fold_rows in by_fold.values()
        for row in _top_tercile(fold_rows)
    ]
    top_r = [float(row["r_multiple"]) for row in top_rows]
    if not all_r or not top_r:
        raise ValueError("OOS predictions are required to compute uplift")
    return (
        sum(top_r) / len(top_r),
        sum(all_r) / len(all_r),
        sum(top_r) / len(top_r) - sum(all_r) / len(all_r),
        len(top_r),
    )


def bootstrap_uplift_interval(
    rows: list[dict[str, Any]],
    *,
    resamples: int = 2000,
    confidence_level: float = 0.95,
    seed: int = 271828,
    block_hours: int = 6,
    min_blocks_per_fold: int = 5,
) -> list[float] | None:
    """Fold-stratified 6h-block bootstrap for Top-Tercile uplift.

    Signals from the same UTC time block are sampled together so a busy market
    period is not incorrectly treated as dozens of independent observations.
    Each fold retains its own number of time blocks and its own ranking cut.
    """
    by_fold: dict[int, dict[int, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    block_hours = int(block_hours)
    min_blocks_per_fold = int(min_blocks_per_fold)
    if block_hours < 1 or min_blocks_per_fold < 1:
        raise ValueError("bootstrap block policy must be positive")
    block_ms = block_hours * 60 * 60 * 1000
    for row in rows:
        timestamp = _timestamp_ms(row.get("signal_timestamp"), "signal_timestamp")
        fold_id = int(row["fold_id"])
        by_fold[fold_id][timestamp // block_ms].append(row)

    if len(by_fold) < 2 or not rows:
        return None
    if any(len(blocks) < min_blocks_per_fold for blocks in by_fold.values()):
        return None

    resamples = int(resamples)
    if resamples < 200:
        raise ValueError("at least 200 bootstrap resamples are required")
    confidence_level = _finite_number(confidence_level, "confidence_level")
    if not 0.5 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0.5 and 1.0")

    rng = random.Random(int(seed))
    fold_blocks = [
        [blocks[key] for key in sorted(blocks)]
        for _, blocks in sorted(by_fold.items())
    ]
    uplift_values = []
    for _ in range(resamples):
        sampled_folds = []
        for blocks in fold_blocks:
            sampled_blocks = [
                rng.choice(blocks)
                for _ in range(len(blocks))
            ]
            sampled_folds.append([
                row for block in sampled_blocks for row in block
            ])
        sampled_all = [row for fold_rows in sampled_folds for row in fold_rows]
        sampled_top = [
            row for fold_rows in sampled_folds
            for row in _top_tercile(fold_rows)
        ]
        if not sampled_all or not sampled_top:
            continue
        all_mean = sum(float(row["r_multiple"]) for row in sampled_all) / len(sampled_all)
        top_mean = sum(float(row["r_multiple"]) for row in sampled_top) / len(sampled_top)
        uplift_values.append(top_mean - all_mean)

    if len(uplift_values) < max(200, resamples // 2):
        return None
    uplift_values.sort()
    tail = (1.0 - confidence_level) / 2.0
    low_index = max(
        0,
        min(len(uplift_values) - 1, int(math.floor(tail * len(uplift_values)))),
    )
    high_index = max(
        0,
        min(len(uplift_values) - 1, int(math.ceil((1.0 - tail) * len(uplift_values)) - 1)),
    )
    return [uplift_values[low_index], uplift_values[high_index]]


def wilson_interval(successes: int, sample_size: int) -> list[float] | None:
    if sample_size <= 0:
        return None
    successes = max(0, min(int(successes), int(sample_size)))
    n = float(sample_size)
    p = successes / n
    z = 1.959963984540054
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    radius = (
        z * math.sqrt((p * (1.0 - p) / n) + z2 / (4.0 * n * n))
        / denominator
    )
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _manifest_rejection_reasons(
    manifest: dict[str, Any],
    group: dict[str, Any],
    dataset_sha256: str,
) -> list[dict[str, str]]:
    reasons = []
    provenance = dict(manifest or {})
    provenance.update(group.get("provenance") or {})

    required_text = (
        "source_provider",
        "source_url",
        "source_license",
        "source_snapshot_utc",
        "source_snapshot_sha256",
        "source_metadata_sha256",
        "raw_dataset_sha256",
        "collector_code_sha",
        "preparation_code_sha",
        "cohort_selection_as_of_utc",
        "data_start_utc",
        "data_end_utc",
        "model_version",
        "feature_schema_version",
        "code_sha",
    )
    for field in required_text:
        if not str(provenance.get(field) or "").strip():
            reasons.append({"code": f"MISSING_{field.upper()}", "detail": f"{field} is required"})

    url = str(provenance.get("source_url") or "")
    if url and not url.startswith(("https://", "http://")):
        reasons.append({"code": "INVALID_SOURCE_URL", "detail": "source_url must be HTTP(S)"})

    source_hash = str(provenance.get("source_snapshot_sha256") or "").lower()
    if source_hash and not SHA256_RE.fullmatch(source_hash):
        reasons.append({"code": "INVALID_SOURCE_SNAPSHOT_SHA256", "detail": "source_snapshot_sha256 must be 64 hexadecimal characters"})
    metadata_hash = str(provenance.get("source_metadata_sha256") or "").lower()
    if metadata_hash and not SHA256_RE.fullmatch(metadata_hash):
        reasons.append({"code": "INVALID_SOURCE_METADATA_SHA256", "detail": "source_metadata_sha256 must be 64 hexadecimal characters"})
    raw_dataset_hash = str(provenance.get("raw_dataset_sha256") or "").lower()
    if raw_dataset_hash and not SHA256_RE.fullmatch(raw_dataset_hash):
        reasons.append({"code": "INVALID_RAW_DATASET_SHA256", "detail": "raw_dataset_sha256 must be 64 hexadecimal characters"})

    if not SHA256_RE.fullmatch(str(dataset_sha256 or "").lower()):
        reasons.append({"code": "INVALID_DATASET_SHA256", "detail": "the exact training dataset file SHA-256 is required"})

    code_sha = str(provenance.get("code_sha") or "").lower()
    if code_sha and not GIT_SHA_RE.fullmatch(code_sha):
        reasons.append({"code": "INVALID_CODE_SHA", "detail": "code_sha must be a Git commit SHA"})
    collector_code_sha = str(provenance.get("collector_code_sha") or "").lower()
    if collector_code_sha and not GIT_SHA_RE.fullmatch(collector_code_sha):
        reasons.append({
            "code": "INVALID_COLLECTOR_CODE_SHA",
            "detail": "collector_code_sha must be a Git commit SHA",
        })
    preparation_code_sha = str(provenance.get("preparation_code_sha") or "").lower()
    if preparation_code_sha and not GIT_SHA_RE.fullmatch(preparation_code_sha):
        reasons.append({
            "code": "INVALID_PREPARATION_CODE_SHA",
            "detail": "preparation_code_sha must be a Git commit SHA",
        })
    model_version = str(provenance.get("model_version") or "")
    feature_version = str(provenance.get("feature_schema_version") or "")
    if model_version and model_version != MODEL_VERSION:
        reasons.append({
            "code": "UNSUPPORTED_MODEL_VERSION",
            "detail": f"expected model_version {MODEL_VERSION}; received {model_version}",
        })
    if feature_version and feature_version != FEATURE_SCHEMA_VERSION:
        reasons.append({
            "code": "FEATURE_SCHEMA_VERSION_MISMATCH",
            "detail": f"expected feature_schema_version {FEATURE_SCHEMA_VERSION}; received {feature_version}",
        })

    config = provenance.get("backtest_config")
    if not isinstance(config, dict):
        reasons.append({"code": "MISSING_BACKTEST_CONFIG", "detail": "backtest_config must be an object"})
    else:
        for field in ("risk_unit_definition", "fee_model", "slippage_model", "entry_rule", "exit_rule"):
            if not str(config.get(field) or "").strip():
                reasons.append({"code": f"MISSING_BACKTEST_{field.upper()}", "detail": f"backtest_config.{field} is required"})
        if not isinstance(config.get("funding_included"), bool):
            reasons.append({"code": "MISSING_FUNDING_POLICY", "detail": "backtest_config.funding_included must be true or false"})
        if "slippage_bps" not in config:
            reasons.append({"code": "MISSING_SLIPPAGE_BPS", "detail": "backtest_config.slippage_bps is required"})
        else:
            try:
                if _finite_number(config["slippage_bps"], "slippage_bps") < 0:
                    raise ValueError("slippage_bps must be non-negative")
            except ValueError as exc:
                reasons.append({"code": "INVALID_SLIPPAGE_BPS", "detail": str(exc)})
        if "fee_rate" not in config and not config.get("fee_model"):
            reasons.append({"code": "MISSING_FEE_MODEL", "detail": "a reproducible fee model is required"})
        elif "fee_rate" in config:
            try:
                if _finite_number(config["fee_rate"], "fee_rate") < 0:
                    raise ValueError("fee_rate must be non-negative")
            except ValueError as exc:
                reasons.append({"code": "INVALID_FEE_RATE", "detail": str(exc)})

    try:
        snapshot_ms = _timestamp_ms(provenance.get("source_snapshot_utc"), "source_snapshot_utc")
        cohort_ms = _timestamp_ms(provenance.get("cohort_selection_as_of_utc"), "cohort_selection_as_of_utc")
        data_start_ms = _timestamp_ms(provenance.get("data_start_utc"), "data_start_utc")
        data_end_ms = _timestamp_ms(provenance.get("data_end_utc"), "data_end_utc")
        if snapshot_ms > cohort_ms:
            reasons.append({
                "code": "COHORT_SELECTION_PRECEDES_SOURCE_SNAPSHOT",
                "detail": "source snapshot must exist no later than the cohort-selection cutoff",
            })
        if cohort_ms >= data_start_ms:
            reasons.append({
                "code": "COHORT_SELECTION_LOOKAHEAD",
                "detail": "cohort selection must be strictly earlier than the first training signal",
            })
        if data_end_ms <= data_start_ms:
            reasons.append({
                "code": "INVALID_DATA_WINDOW",
                "detail": "data_end_utc must be later than data_start_utc",
            })
    except ValueError as exc:
        # Missing/invalid timestamp errors are already reported above where appropriate.
        if not any(item["code"].startswith("MISSING_") for item in reasons):
            reasons.append({"code": "INVALID_PROVENANCE_TIMESTAMP", "detail": str(exc)})

    return reasons


def evaluate_approval(
    *,
    group: dict[str, Any],
    walk_forward: dict[str, Any],
    manifest: dict[str, Any],
    dataset_sha256: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return explicit approval/disable reasons for one independent market group."""
    policy = {**DEFAULT_POLICY, **(policy or {})}
    group_key = {
        "exchange": str(group.get("exchange") or ""),
        "symbol": str(group.get("symbol") or ""),
        "market_type": str(group.get("market_type") or ""),
        "timeframe": str(group.get("timeframe") or ""),
    }
    reasons = _manifest_rejection_reasons(manifest, group, dataset_sha256)
    if not all(group_key.values()):
        reasons.append({
            "code": "INCOMPLETE_GROUP_KEY",
            "detail": "exchange, symbol, market_type and timeframe are all required",
        })

    train_count = int(walk_forward.get("min_training_samples_per_fold", 0) or 0)
    fold_count = int(walk_forward.get("fold_count", 0) or 0)
    predictions = walk_forward.get("oos_predictions") or []
    folds = walk_forward.get("folds") or []
    oos_count = len(predictions)
    oos_signals = sum(int(row.get("test_signal_count", 0) or 0) for row in folds)
    if int(walk_forward.get("oos_signals", -1)) != oos_signals:
        reasons.append({
            "code": "OOS_SIGNAL_COUNT_MISMATCH",
            "detail": (
                f"reported OOS signals {walk_forward.get('oos_signals')} do not match "
                f"the sum of fold test signals {oos_signals}"
            ),
        })
    if int(walk_forward.get("oos_samples", -1)) != oos_count:
        reasons.append({
            "code": "OOS_SAMPLE_COUNT_MISMATCH",
            "detail": (
                f"reported OOS samples {walk_forward.get('oos_samples')} do not match "
                f"prediction rows {oos_count}"
            ),
        })
    fold_oos_excluded = sum(
        int(row.get("excluded_test_samples", 0) or 0) for row in folds
    )
    fold_oos_excluded_reasons: dict[str, int] = {}
    for fold in folds:
        for reason, count in (fold.get("excluded_test_reasons") or {}).items():
            fold_oos_excluded_reasons[reason] = (
                fold_oos_excluded_reasons.get(reason, 0) + int(count)
            )
    if int(walk_forward.get("oos_excluded_samples", -1)) != fold_oos_excluded:
        reasons.append({
            "code": "OOS_EXCLUDED_COUNT_MISMATCH",
            "detail": (
                f"reported OOS exclusions {walk_forward.get('oos_excluded_samples')} "
                f"do not match the sum of per-fold excluded rows {fold_oos_excluded}"
            ),
        })
    if oos_signals != oos_count + fold_oos_excluded:
        reasons.append({
            "code": "OOS_OUTCOME_PARTITION_MISMATCH",
            "detail": (
                f"OOS signals {oos_signals} must equal eligible outcomes {oos_count} "
                f"plus excluded outcomes {fold_oos_excluded}"
            ),
        })
    if dict(walk_forward.get("oos_excluded_reasons") or {}) != dict(sorted(fold_oos_excluded_reasons.items())):
        reasons.append({
            "code": "OOS_EXCLUDED_REASON_MISMATCH",
            "detail": "OOS excluded reason totals do not match the per-fold reason counts",
        })
    if len(folds) != fold_count:
        reasons.append({
            "code": "WALK_FORWARD_FOLD_COUNT_MISMATCH",
            "detail": f"fold_count={fold_count}, but {len(folds)} fold reports were supplied",
        })

    if train_count < int(policy["min_training_samples_per_fold"]):
        reasons.append({
            "code": "INSUFFICIENT_TRAINING_SAMPLES",
            "detail": (
                f"minimum fully matured training examples in every fold is "
                f"{policy['min_training_samples_per_fold']}; observed {train_count}"
            ),
        })
    if fold_count < int(policy["min_walk_forward_folds"]):
        reasons.append({
            "code": "INSUFFICIENT_WALK_FORWARD_FOLDS",
            "detail": f"minimum is {policy['min_walk_forward_folds']}; observed {fold_count}",
        })
    if oos_signals < int(policy["min_oos_signals"]):
        reasons.append({
            "code": "INSUFFICIENT_OOS_SIGNALS",
            "detail": f"minimum is {policy['min_oos_signals']}; observed {oos_signals}",
        })
    if oos_count < int(policy["min_oos_samples"]):
        reasons.append({
            "code": "INSUFFICIENT_OOS_SAMPLES",
            "detail": f"minimum is {policy['min_oos_samples']}; observed {oos_count}",
        })

    training_counts = [int(row.get("training_samples", 0)) for row in folds]
    if (
        len(training_counts) < int(policy["min_walk_forward_folds"])
        or any(count < int(policy["min_training_samples_per_fold"]) for count in training_counts)
    ):
        reasons.append({
            "code": "PER_FOLD_TRAINING_COUNT_MISMATCH",
            "detail": (
                f"every fold must report at least {policy['min_training_samples_per_fold']} "
                f"training rows; observed {training_counts}"
            ),
        })

    per_fold_signals = [int(row.get("test_signal_count", 0)) for row in folds]
    if (
        len(per_fold_signals) < int(policy["min_walk_forward_folds"])
        or any(count < int(policy["min_oos_signals_per_fold"]) for count in per_fold_signals)
    ):
        reasons.append({
            "code": "INSUFFICIENT_OOS_SIGNALS_PER_FOLD",
            "detail": (
                f"each OOS fold must contain at least {policy['min_oos_signals_per_fold']} "
                f"signal rows; observed {per_fold_signals}"
            ),
        })

    per_fold_samples = [int(row.get("test_samples", 0)) for row in folds]
    if (
        len(per_fold_samples) < int(policy["min_walk_forward_folds"])
        or any(count < int(policy["min_oos_samples_per_fold"]) for count in per_fold_samples)
    ):
        reasons.append({
            "code": "INSUFFICIENT_OOS_SAMPLES_PER_FOLD",
            "detail": (
                f"each OOS fold must contain at least {policy['min_oos_samples_per_fold']} "
                f"eligible outcomes; observed {per_fold_samples}"
            ),
        })

    sorted_folds = sorted(folds, key=lambda row: int(row.get("fold_id", 0)))
    if [int(row.get("fold_id", 0)) for row in sorted_folds] != list(range(1, fold_count + 1)):
        reasons.append({
            "code": "INVALID_FOLD_SEQUENCE",
            "detail": "fold IDs must be the contiguous sequence 1..fold_count",
        })
    previous_test_end = None
    for fold in sorted_folds:
        try:
            test_start = _timestamp_ms(fold.get("test_start_utc"), "test_start_utc")
            test_end = _timestamp_ms(fold.get("test_end_utc"), "test_end_utc")
            label_end = _timestamp_ms(fold.get("training_label_end_utc"), "training_label_end_utc")
            if test_end < test_start or label_end >= test_start:
                raise ValueError("invalid test period or training label not matured before test start")
            if previous_test_end is not None and test_start <= previous_test_end:
                raise ValueError("walk-forward test windows overlap or are not chronological")
            previous_test_end = test_end
        except (TypeError, ValueError) as exc:
            reasons.append({"code": "INVALID_WALK_FORWARD_CHRONOLOGY", "detail": str(exc)})
            break

    excluded_reasons = dict(sorted(fold_oos_excluded_reasons.items()))
    excluded_count = fold_oos_excluded
    input_excluded_reasons = dict(walk_forward.get("input_excluded_reasons") or {})
    input_excluded_count = int(walk_forward.get("input_excluded_samples", 0) or 0)

    preparation_excluded_count = int(group.get("preparation_excluded_samples", 0) or 0)
    preparation_excluded_reasons = dict(
        group.get("preparation_excluded_reasons") or {}
    )
    prepared_signal_count = (
        len(group.get("samples") or [])
        if "samples" in group else oos_count
    )
    positions_seen = int(
        group.get(
            "positions_seen",
            prepared_signal_count + preparation_excluded_count,
        )
        or 0
    )
    preparation_coverage_pct = (
        prepared_signal_count / positions_seen * 100.0
        if positions_seen > 0 else 0.0
    )
    if positions_seen != prepared_signal_count + preparation_excluded_count:
        reasons.append({
            "code": "PREPARATION_SAMPLE_PARTITION_MISMATCH",
            "detail": (
                f"positions_seen={positions_seen} must equal prepared signals "
                f"{prepared_signal_count} plus preparation exclusions "
                f"{preparation_excluded_count}"
            ),
        })
    if sum(int(value) for value in preparation_excluded_reasons.values()) != preparation_excluded_count:
        reasons.append({
            "code": "PREPARATION_EXCLUSION_REASON_MISMATCH",
            "detail": (
                f"preparation exclusion reasons sum to "
                f"{sum(int(value) for value in preparation_excluded_reasons.values())}, "
                f"but preparation_excluded_samples={preparation_excluded_count}"
            ),
        })
    if positions_seen > 0 and preparation_coverage_pct < float(policy["min_preparation_coverage_pct"]):
        reasons.append({
            "code": "LOW_PREPARATION_FEATURE_COVERAGE",
            "detail": (
                f"prepared feature coverage must be at least "
                f"{policy['min_preparation_coverage_pct']:.1f}%; observed "
                f"{preparation_coverage_pct:.2f}% "
                f"({prepared_signal_count}/{positions_seen})"
            ),
        })

    metrics = {
        "oos_signals": oos_signals,
        "oos_samples": oos_count,
        "walk_forward_folds": fold_count,
        "training_samples_per_fold": training_counts,
        "min_training_samples_per_fold": train_count,
        "eligible_samples": oos_count,
        "excluded_samples": excluded_count,
        "excluded_sample_reasons": excluded_reasons,
        "input_excluded_samples": input_excluded_count,
        "input_excluded_sample_reasons": input_excluded_reasons,
        "positions_seen_before_feature_preparation": positions_seen,
        "prepared_signal_samples": prepared_signal_count,
        "preparation_excluded_samples": preparation_excluded_count,
        "preparation_excluded_sample_reasons": preparation_excluded_reasons,
        "preparation_coverage_pct": preparation_coverage_pct,
        "mean_r": None,
        "positive_r_share": None,
        "wilson_ci_positive_r_share_95": None,
        "top_tercile_mean_r": None,
        "uplift": None,
        "uplift_confidence_interval_95": None,
        "top_tercile_samples": 0,
    }

    valid_prediction_rows = []
    seen_ids = set()
    fold_by_id = {int(row.get("fold_id", 0)): row for row in sorted_folds}
    for row in predictions:
        try:
            predicted = _finite_number(row.get("predicted_r_multiple"), "predicted_r_multiple")
            actual = _finite_number(row.get("r_multiple"), "r_multiple")
            fold_id = int(row.get("fold_id"))
            sample_id = str(row.get("sample_id") or "")
            if not sample_id or fold_id < 1 or sample_id in seen_ids:
                raise ValueError("each OOS prediction needs a unique sample_id and positive fold_id")
            seen_ids.add(sample_id)
            if fold_id not in fold_by_id:
                raise ValueError(f"prediction {sample_id} refers to an unknown fold")
            signal_ms = _timestamp_ms(row.get("signal_timestamp"), "prediction.signal_timestamp")
            exit_ms = _timestamp_ms(row.get("exit_timestamp"), "prediction.exit_timestamp")
            fold = fold_by_id[fold_id]
            test_start = _timestamp_ms(fold.get("test_start_utc"), "fold.test_start_utc")
            test_end = _timestamp_ms(fold.get("test_end_utc"), "fold.test_end_utc")
            if not test_start <= signal_ms <= test_end or exit_ms < signal_ms:
                raise ValueError(f"prediction {sample_id} falls outside its OOS fold")
            if str(row.get("exit_reason") or "").upper() not in ELIGIBLE_EXIT_REASONS:
                raise ValueError(f"prediction {sample_id} has an ineligible exit reason")
            valid_prediction_rows.append({
                "sample_id": sample_id,
                "fold_id": fold_id,
                "signal_timestamp": row["signal_timestamp"],
                "exit_timestamp": row["exit_timestamp"],
                "predicted_r_multiple": predicted,
                "r_multiple": actual,
            })
        except (TypeError, ValueError) as exc:
            reasons.append({"code": "INVALID_OOS_PREDICTION", "detail": str(exc)})
            break

    fold_prediction_counts = {fold_id: 0 for fold_id in fold_by_id}
    fold_time_blocks: dict[int, set[int]] = {
        fold_id: set() for fold_id in fold_by_id
    }
    block_hours = int(policy["bootstrap_block_hours"])
    block_ms = block_hours * 60 * 60 * 1000
    for row in valid_prediction_rows:
        fold_prediction_counts[row["fold_id"]] += 1
        signal_ms = _timestamp_ms(row["signal_timestamp"], "prediction.signal_timestamp")
        fold_time_blocks[row["fold_id"]].add(signal_ms // block_ms)
    time_block_counts = [
        len(fold_time_blocks[fold_id])
        for fold_id in sorted(fold_time_blocks)
    ]
    metrics["oos_time_blocks_per_fold"] = time_block_counts
    if (
        len(time_block_counts) < int(policy["min_walk_forward_folds"])
        or any(count < int(policy["min_oos_time_blocks_per_fold"]) for count in time_block_counts)
    ):
        reasons.append({
            "code": "INSUFFICIENT_OOS_TIME_BLOCKS",
            "detail": (
                f"each OOS fold must contain at least {policy['min_oos_time_blocks_per_fold']} "
                f"distinct {block_hours}h UTC blocks; observed {time_block_counts}"
            ),
        })
    for fold_id, fold in fold_by_id.items():
        if fold_prediction_counts.get(fold_id, 0) != int(fold.get("test_samples", 0)):
            reasons.append({
                "code": "FOLD_PREDICTION_COUNT_MISMATCH",
                "detail": (
                    f"fold {fold_id} reports {fold.get('test_samples')} outcomes but "
                    f"contains {fold_prediction_counts.get(fold_id, 0)} predictions"
                ),
            })

    if valid_prediction_rows:
        try:
            top_mean, all_mean, uplift_value, top_count = _uplift(valid_prediction_rows)
            metrics.update({
                "mean_r": all_mean,
                "positive_r_share": sum(row["r_multiple"] > 0 for row in valid_prediction_rows) / len(valid_prediction_rows),
                "wilson_ci_positive_r_share_95": wilson_interval(
                    sum(row["r_multiple"] > 0 for row in valid_prediction_rows),
                    len(valid_prediction_rows),
                ),
                "top_tercile_mean_r": top_mean,
                "uplift": uplift_value,
                "top_tercile_samples": top_count,
            })
            ci = bootstrap_uplift_interval(
                valid_prediction_rows,
                resamples=int(policy["bootstrap_resamples"]),
                confidence_level=float(policy["confidence_level"]),
                seed=int(policy["bootstrap_seed"]),
                block_hours=int(policy["bootstrap_block_hours"]),
                min_blocks_per_fold=int(policy["min_oos_time_blocks_per_fold"]),
            )
            metrics["uplift_confidence_interval_95"] = ci
            if top_count < int(policy["min_top_tercile_samples"]):
                reasons.append({
                    "code": "INSUFFICIENT_TOP_TERCILE_SAMPLES",
                    "detail": (
                        f"minimum is {policy['min_top_tercile_samples']}; "
                        f"observed {top_count}"
                    ),
                })
            if uplift_value <= 0:
                reasons.append({
                    "code": "NON_POSITIVE_TOP_TERCILE_UPLIFT",
                    "detail": f"top-tercile mean R must exceed all-OOS mean R; uplift={uplift_value:.6f}",
                })
            if ci is None or ci[0] <= 0:
                reasons.append({
                    "code": "UPLIFT_CI_INCLUDES_ZERO",
                    "detail": (
                        f"the lower bound of the {policy['confidence_level']:.0%} uplift CI "
                        f"must be greater than zero; interval={ci}"
                    ),
                })
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            reasons.append({"code": "UPLIFT_EVALUATION_FAILED", "detail": str(exc)})
    else:
        reasons.append({
            "code": "NO_OOS_PREDICTIONS",
            "detail": "no valid chronological OOS predictions were supplied",
        })

    return {
        "group": group_key,
        "model_status": "APPROVED" if not reasons else "DISABLED",
        "approved": not reasons,
        "approval_scope": "RESEARCH_ONLY_EXTERNAL_TRADER_POSITION_OUTCOMES",
        "deployment_status": "RESEARCH_ONLY",
        "runtime_eligible": False,
        "runtime_block_reason": (
            "Training examples are public positions from a preselected external-wallet cohort, "
            "not signals generated by this project's scanner/backtest. An untouched paper-trading "
            "validation on project-generated signals is required before runtime Advisor integration."
        ),
        "rejection_reasons": reasons,
        "metrics": metrics,
        "policy": policy,
        "provenance": {
            key: value
            for key, value in {
                **(manifest or {}),
                **(group.get("provenance") or {}),
                "dataset_sha256": dataset_sha256,
            }.items()
            if key in (
                "source_provider", "source_url", "source_license",
                "source_snapshot_utc", "leaderboard_retrieved_at_utc",
                "source_snapshot_sha256", "source_metadata_sha256",
                "raw_dataset_sha256", "collector_code_sha", "preparation_code_sha",
                "cohort_selection_as_of_utc",
                "data_start_utc", "data_end_utc", "dataset_sha256",
                "model_version", "feature_schema_version", "code_sha",
                "backtest_config",
            )
        },
    }
def train_validate_group(
    *,
    group: dict[str, Any],
    manifest: dict[str, Any],
    dataset_sha256: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train, evaluate, and return an approved final model only when all gates pass."""
    policy = {**DEFAULT_POLICY, **(policy or {})}
    samples = group.get("samples") or []
    expected_group = {
        "exchange": str(group.get("exchange") or ""),
        "symbol": str(group.get("symbol") or ""),
        "market_type": str(group.get("market_type") or ""),
        "timeframe": str(group.get("timeframe") or ""),
    }
    if not all(expected_group.values()):
        raise ValueError("group must declare exchange, symbol, market_type and timeframe")
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError(f"sample {index} must be an object")
        for field, expected in expected_group.items():
            if str(sample.get(field) or "") != expected:
                raise ValueError(
                    f"sample {index} has {field}={sample.get(field)!r}; expected {expected!r}"
                )
    ordered_samples = _validate_samples(samples)

    provenance = dict(manifest or {})
    provenance.update(group.get("provenance") or {})
    window_issues = []
    try:
        cohort_ms = _timestamp_ms(
            provenance.get("cohort_selection_as_of_utc"), "cohort_selection_as_of_utc"
        )
        data_start_ms = _timestamp_ms(provenance.get("data_start_utc"), "data_start_utc")
        data_end_ms = _timestamp_ms(provenance.get("data_end_utc"), "data_end_utc")
        for row in ordered_samples:
            if row["signal_timestamp_ms"] <= cohort_ms:
                window_issues.append({
                    "code": "SAMPLE_PRECEDES_COHORT_CUTOFF",
                    "detail": f"sample {row['sample_id']} is not after the cohort-selection cutoff",
                })
            if (
                row["signal_timestamp_ms"] < data_start_ms
                or row["exit_timestamp_ms"] > data_end_ms
            ):
                window_issues.append({
                    "code": "SAMPLE_OUTSIDE_MANIFEST_DATA_WINDOW",
                    "detail": f"sample {row['sample_id']} falls outside the declared data window",
                })
            if window_issues:
                break
    except ValueError as exc:
        window_issues.append({"code": "INVALID_GROUP_DATA_WINDOW", "detail": str(exc)})

    walk_forward = walk_forward_predict(
        samples,
        min_training_samples=int(policy["min_training_samples_per_fold"]),
        min_folds=int(policy["min_walk_forward_folds"]),
        max_folds=int(policy["max_walk_forward_folds"]),
        min_test_samples_per_fold=int(policy["min_oos_samples_per_fold"]),
        min_test_signals_per_fold=int(policy["min_oos_signals_per_fold"]),
        ridge_alpha=float(policy["ridge_alpha"]),
    )
    approval = evaluate_approval(
        group=group,
        walk_forward=walk_forward,
        manifest=manifest,
        dataset_sha256=dataset_sha256,
        policy=policy,
    )
    if window_issues:
        approval["rejection_reasons"].extend(window_issues)
        approval["approved"] = False
        approval["model_status"] = "DISABLED"

    result = {
        "approval": approval,
        "walk_forward": {
            key: value for key, value in walk_forward.items()
            if key != "final_candidate_model"
        },
        "model": None,
    }
    if approval["approved"]:
        result["model"] = {
            **walk_forward["final_candidate_model"],
            "group": approval["group"],
            "manifest": approval["provenance"],
            "approval_metrics": approval["metrics"],
            "approval_policy": approval["policy"],
            "deployment_status": approval["deployment_status"],
            "runtime_eligible": approval["runtime_eligible"],
            "runtime_block_reason": approval["runtime_block_reason"],
        }
    return result
