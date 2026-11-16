"""Train separate research models from prospective OKX lead-trader outcomes.

The target is OKX pnlRatio (an ROI-style ratio), not true risk-multiple R.
Models produced here are research-only and are not connected to the runtime
Advisor or eligible for the strategy's R-multiple approval gate.
"""
import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.advisor_model import (
    FEATURE_SCHEMA_VERSION,
    fit_ridge_on_targets,
    predict_target,
)


MODEL_VERSION = "okx-copy-leader-pnlratio-ridge-1.0.0"
MIN_TRAINING_SAMPLES = 200
MIN_OOS_SAMPLES = 50
BOOTSTRAP_REPLICATES = 1000
BOOTSTRAP_SEED = 880_231


def _iso_utc(timestamp_ms):
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).isoformat()


def _mean(values):
    return sum(values) / len(values) if values else None


def _top_tercile_uplift(rows):
    ordered = sorted(rows, key=lambda row: float(row["predicted_pnl_ratio"]), reverse=True)
    top_count = max(1, math.ceil(len(ordered) / 3.0))
    all_mean = _mean([float(row["pnl_ratio"]) for row in rows])
    top_mean = _mean([float(row["pnl_ratio"]) for row in ordered[:top_count]])
    return top_mean, all_mean, top_mean - all_mean, top_count


def _bootstrap_uplift_ci(rows, replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    if len(rows) < 3:
        return None
    import random

    ordered = sorted(rows, key=lambda row: int(row["open_time_ms"]))
    count = len(ordered)
    block_length = max(2, int(round(count ** (1.0 / 3.0))))
    rng = random.Random(seed)
    distribution = []
    for _ in range(int(replicates)):
        indices = []
        while len(indices) < count:
            start = rng.randrange(count)
            indices.extend((start + offset) % count for offset in range(block_length))
        sampled = [ordered[index] for index in indices[:count]]
        _, _, uplift, _ = _top_tercile_uplift(sampled)
        distribution.append(uplift)
    distribution.sort()
    lower = distribution[max(0, int(math.floor(0.025 * len(distribution))))]
    upper_index = min(len(distribution) - 1, int(math.ceil(0.975 * len(distribution)) - 1))
    return [round(lower, 8), round(distribution[upper_index], 8)]


def _load_groups(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT unique_code, sub_pos_id, inst_id, inst_type, open_time_ms,
                   close_time_ms, pnl, pnl_ratio, features_json
            FROM lead_positions
            WHERE prospective_eligible=1 AND research_only=0
              AND features_json IS NOT NULL AND pnl_ratio IS NOT NULL
            ORDER BY inst_id, inst_type, open_time_ms, sub_pos_id
            """
        ).fetchall()
    finally:
        connection.close()

    groups = defaultdict(list)
    for row in rows:
        feature_payload = json.loads(row["features_json"])
        features = feature_payload.get("features")
        if not isinstance(features, list) or len(features) != 5:
            continue
        groups[(row["inst_id"], row["inst_type"], feature_payload.get("timeframe", "1H"))].append({
            "unique_code": row["unique_code"],
            "sub_pos_id": row["sub_pos_id"],
            "inst_id": row["inst_id"],
            "inst_type": row["inst_type"],
            "open_time_ms": int(row["open_time_ms"]),
            "close_time_ms": int(row["close_time_ms"]),
            "pnl": float(row["pnl"]),
            "pnl_ratio": float(row["pnl_ratio"]),
            "features": [float(value) for value in features],
        })
    return groups


def train_okx_expert_models(
    db_path,
    *,
    output_path="artifacts/okx-expert-models.json",
    min_training_samples=MIN_TRAINING_SAMPLES,
    min_oos_samples=MIN_OOS_SAMPLES,
    bootstrap_replicates=BOOTSTRAP_REPLICATES,
):
    groups = _load_groups(db_path)
    report_groups = []
    for (inst_id, inst_type, timeframe), rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row["open_time_ms"], row["sub_pos_id"]))
        group_id = f"okx:{inst_id}:{inst_type}:{timeframe}"
        reasons = []
        model = None
        predictions = []
        if len(rows) < min_training_samples + min_oos_samples:
            report_groups.append({
                "group_id": group_id,
                "model_status": "DISABLED",
                "approval_status": "RESEARCH_ONLY",
                "reason": (
                    f"INSUFFICIENT_PROSPECTIVE_SAMPLES: need at least "
                    f"{min_training_samples + min_oos_samples}, received {len(rows)}"
                ),
                "prospective_sample_count": len(rows),
                "target": "leader_pnl_ratio_not_R",
            })
            continue

        split_index = int(len(rows) * 0.8)
        test_start_ms = rows[split_index]["open_time_ms"]
        train_rows = [
            row for row in rows[:split_index]
            if row["close_time_ms"] < test_start_ms
        ]
        test_rows = rows[split_index:]

        if len(train_rows) < min_training_samples:
            reasons.append("PROSPECTIVE_TRAINING_SAMPLES_BELOW_200")
        if len(test_rows) < min_oos_samples:
            reasons.append("PROSPECTIVE_OOS_SAMPLES_BELOW_50")

        try:
            if not reasons:
                model = fit_ridge_on_targets(
                    train_rows,
                    target_key="pnl_ratio",
                    target_name="leader_pnl_ratio_not_R",
                    model_version=MODEL_VERSION,
                )
                for row in test_rows:
                    predictions.append({
                        **row,
                        "predicted_pnl_ratio": predict_target(model, row),
                    })
        except (ValueError, TypeError, OverflowError) as error:
            reasons.append(f"MODEL_FIT_OR_PREDICTION_ERROR: {type(error).__name__}: {error}")

        top_mean = all_mean = uplift = None
        uplift_ci = None
        top_count = 0
        if predictions:
            top_mean, all_mean, uplift, top_count = _top_tercile_uplift(predictions)
            uplift_ci = _bootstrap_uplift_ci(
                predictions,
                replicates=bootstrap_replicates,
            )
            if uplift <= 0:
                reasons.append("EXPERT_TOP_TERCILE_UPLIFT_NOT_POSITIVE")
            if not uplift_ci or uplift_ci[0] <= 0:
                reasons.append("EXPERT_UPLIFT_95_CI_LOWER_BOUND_NOT_ABOVE_ZERO")

        canonical_data = [
            {
                "sub_pos_id": row["sub_pos_id"],
                "open_time_ms": row["open_time_ms"],
                "close_time_ms": row["close_time_ms"],
                "pnl": row["pnl"],
                "pnl_ratio": row["pnl_ratio"],
                "features": row["features"],
            }
            for row in rows
        ]
        dataset_sha256 = hashlib.sha256(
            json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        metadata = {
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "algorithm": "ridge_regression_l2",
            "hyperparameters": {"alpha": 2.0},
            "target": "leader_pnl_ratio_not_R",
            "data_source": {
                "provider": "OKX Copy Trading public API",
                "instrument": inst_id,
                "inst_type": inst_type,
                "timeframe": timeframe,
                "cohort_rule": "qualified lead trader observed prospectively before trade open",
            },
            "dataset_sha256": dataset_sha256,
            "period_utc": {
                "start": _iso_utc(rows[0]["open_time_ms"]),
                "end": _iso_utc(rows[-1]["close_time_ms"]),
            },
            "group_id": group_id,
            "training_start_utc": _iso_utc(train_rows[0]["open_time_ms"]) if train_rows else None,
            "training_end_utc": _iso_utc(max((row["close_time_ms"] for row in train_rows), default=0)) if train_rows else None,
            "test_start_utc": _iso_utc(test_rows[0]["open_time_ms"]) if test_rows else None,
            "test_end_utc": _iso_utc(test_rows[-1]["close_time_ms"]) if test_rows else None,
            "training_samples": len(train_rows),
            "oos_samples": len(predictions),
        }
        report_groups.append({
            "group_id": group_id,
            "model_status": (
                "RESEARCH_ONLY_SIGNAL_POSITIVE"
                if model is not None and uplift is not None and uplift > 0 and uplift_ci and uplift_ci[0] > 0
                else "RESEARCH_ONLY"
            ),
            "approval_status": "RESEARCH_ONLY_NOT_R_MULTIPLE",
            "approval_reasons": reasons,
            "prospective_sample_count": len(rows),
            "training_samples": len(train_rows),
            "oos_samples": len(predictions),
            "top_tercile_sample_count": top_count,
            "top_tercile_mean_pnl_ratio": top_mean,
            "all_oos_mean_pnl_ratio": all_mean,
            "uplift_pnl_ratio": uplift,
            "uplift_confidence_interval_95": uplift_ci,
            "model_metadata": metadata,
            "model": model,
        })

    report = {
        "report_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "OKX Copy Trading public leader and completed lead-position endpoints",
        "target_warning": (
            "OKX pnlRatio is not true R. This auxiliary expert-outcome model is "
            "not eligible for the core Advisor net-R model approval or runtime use."
        ),
        "approval_policy": {
            "minimum_training_samples_per_instrument_group": min_training_samples,
            "minimum_chronological_oos_samples": min_oos_samples,
            "uplift_lower_95_ci_must_be_positive_for_research_signal": True,
            "runtime_activation": False,
        },
        "groups": report_groups,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/okx-expert-trades.sqlite3")
    parser.add_argument("--output", default="artifacts/okx-expert-models.json")
    parser.add_argument("--min-training-samples", type=int, default=MIN_TRAINING_SAMPLES)
    parser.add_argument("--min-oos-samples", type=int, default=MIN_OOS_SAMPLES)
    args = parser.parse_args()
    report = train_okx_expert_models(
        args.db_path,
        output_path=args.output,
        min_training_samples=args.min_training_samples,
        min_oos_samples=args.min_oos_samples,
    )
    print(json.dumps({
        "output": args.output,
        "groups": len(report["groups"]),
        "statuses": {
            status: sum(group["model_status"] == status for group in report["groups"])
            for status in sorted({group["model_status"] for group in report["groups"]})
        },
    }, indent=2))


if __name__ == "__main__":
    main()
