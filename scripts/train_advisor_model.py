#!/usr/bin/env python3
"""Train and validate per-market Advisor R-multiple models from a frozen dataset.

Input is a JSON file containing provenance metadata and independent
exchange/symbol/market/timeframe groups. A group model is exported only when
its own chronological OOS validation passes every approval gate.
"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.advisor_model_validation import train_validate_group


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Frozen JSON dataset with manifest and groups")
    parser.add_argument("--out-dir", default="artifacts/advisor-models")
    parser.add_argument("--code-sha", required=True, help="Git commit SHA of the training/approval code")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    raw_bytes = dataset_path.read_bytes()
    dataset_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = payload.get("manifest") or {}
    manifest["preparation_code_sha"] = (
        manifest.get("preparation_code_sha") or manifest.get("code_sha") or ""
    )
    manifest["code_sha"] = args.code_sha

    reports = []
    models = []
    errors = []
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise SystemExit("dataset must contain a non-empty 'groups' list")

    for group in groups:
        group_key = {
            "exchange": group.get("exchange"),
            "symbol": group.get("symbol"),
            "market_type": group.get("market_type"),
            "timeframe": group.get("timeframe"),
        }
        try:
            trained = train_validate_group(
                group=group,
                manifest=manifest,
                dataset_sha256=dataset_sha256,
            )
            model = trained["model"]
            model_file = None
            if model is not None:
                safe_key = "_".join(
                    str(group_key[key]).replace("/", "-").replace(":", "-")
                    for key in ("exchange", "symbol", "market_type", "timeframe")
                )
                model_file = f"advisor-model-{safe_key}.json"
                models.append((model_file, model))

            walk_forward = trained["walk_forward"]
            predictions_json = json.dumps(
                walk_forward.get("oos_predictions", []),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            prep_excluded = int(group.get("preparation_excluded_samples", 0) or 0)
            prep_excluded_reasons = dict(group.get("preparation_excluded_reasons") or {})
            trained["approval"]["metrics"].update({
                "positions_seen_before_feature_preparation": int(group.get("positions_seen", len(group.get("samples") or []))),
                "preparation_excluded_samples": prep_excluded,
                "preparation_excluded_sample_reasons": prep_excluded_reasons,
                "prepared_signal_samples": len(group.get("samples") or []),
            })
            reports.append({
                "group": group_key,
                "positions_seen_before_feature_preparation": int(group.get("positions_seen", len(group.get("samples") or []))),
                "preparation_excluded_samples": prep_excluded,
                "preparation_excluded_sample_reasons": prep_excluded_reasons,
                "prepared_signal_samples": len(group.get("samples") or []),
                "approval": trained["approval"],
                "model_artifact": model_file,
                "walk_forward": {
                    "fold_count": walk_forward["fold_count"],
                    "min_training_samples_per_fold": walk_forward["min_training_samples_per_fold"],
                    "total_training_rows_used_across_folds": walk_forward["total_training_rows_used_across_folds"],
                    "oos_signals": walk_forward.get("oos_signals", 0),
                    "oos_outcome_samples": walk_forward.get("oos_samples", 0),
                    "input_signals": walk_forward.get("input_signals", 0),
                    "input_eligible_samples": walk_forward.get("input_eligible_samples", 0),
                    "oos_excluded_samples": walk_forward.get("oos_excluded_samples", 0),
                    "oos_excluded_reasons": walk_forward.get("oos_excluded_reasons", {}),
                    "input_excluded_samples": walk_forward.get("input_excluded_samples", 0),
                    "input_excluded_reasons": walk_forward.get("input_excluded_reasons", {}),
                    "oos_prediction_count": len(walk_forward.get("oos_predictions", [])),
                    "oos_predictions_sha256": hashlib.sha256(predictions_json).hexdigest(),
                    "folds": walk_forward.get("folds", []),
                },
            })
        except Exception as exc:
            source_samples = group.get("samples") or []
            exit_reasons = Counter(
                str(row.get("exit_reason") or "UNKNOWN").upper()
                for row in source_samples
            )
            eligible_count = sum(
                count for reason, count in exit_reasons.items()
                if reason in {"STOP", "STOP_GAP", "TARGET", "TARGET_GAP", "TIME_EXIT"}
            )
            disabled_code = "TRAINING_OR_VALIDATION_FAILED"
            detail = f"{type(exc).__name__}: {exc}"
            if "insufficient eligible training labels" in str(exc).lower():
                disabled_code = "INSUFFICIENT_TRAINING_SAMPLES"
            elif "insufficient signals for walk-forward" in str(exc).lower():
                disabled_code = "INSUFFICIENT_OOS_SIGNALS"
            elif "fully matured eligible training labels" in str(exc).lower():
                disabled_code = "INSUFFICIENT_MATURED_TRAINING_LABELS"
            reports.append({
                "group": group_key,
                "positions_seen_before_feature_preparation": int(group.get("positions_seen", len(source_samples))),
                "preparation_excluded_samples": int(group.get("preparation_excluded_samples", 0) or 0),
                "preparation_excluded_sample_reasons": dict(group.get("preparation_excluded_reasons") or {}),
                "prepared_signal_samples": len(source_samples),
                "approval": {
                    "model_status": "DISABLED",
                    "approved": False,
                    "rejection_reasons": [{
                        "code": disabled_code,
                        "detail": detail,
                    }],
                    "metrics": {
                        "input_signals": len(source_samples),
                        "input_eligible_samples": eligible_count,
                        "input_excluded_samples": len(source_samples) - eligible_count,
                        "input_excluded_sample_reasons": {
                            reason: count
                            for reason, count in sorted(exit_reasons.items())
                            if reason not in {"STOP", "STOP_GAP", "TARGET", "TARGET_GAP", "TIME_EXIT"}
                        },
                    },
                },
                "model_artifact": None,
                "walk_forward": None,
            })
            errors.append({"group": group_key, "error": detail})

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "report_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_file": dataset_path.name,
        "dataset_sha256": dataset_sha256,
        "model_version": manifest.get("model_version"),
        "feature_schema_version": manifest.get("feature_schema_version"),
        "source_provider": manifest.get("source_provider"),
        "source_url": manifest.get("source_url"),
        "source_metadata_url": manifest.get("source_metadata_url"),
        "source_license": manifest.get("source_license"),
        "source_snapshot_utc": manifest.get("source_snapshot_utc"),
        "source_snapshot_sha256": manifest.get("source_snapshot_sha256"),
        "source_metadata_sha256": manifest.get("source_metadata_sha256"),
        "raw_dataset_sha256": manifest.get("raw_dataset_sha256"),
        "collector_code_sha": manifest.get("collector_code_sha"),
        "preparation_code_sha": manifest.get("preparation_code_sha"),
        "training_code_sha": manifest.get("code_sha"),
        "approval_scope": "RESEARCH_ONLY_EXTERNAL_TRADER_POSITION_OUTCOMES",
        "runtime_eligible": False,
        "runtime_activation": "BLOCKED_PENDING_PROJECT_SCANNER_PAPER_VALIDATION",
        "groups_requested": len(groups),
        "groups_approved": sum(1 for row in reports if (row.get("approval") or {}).get("approved")),
        "groups_disabled": sum(1 for row in reports if not (row.get("approval") or {}).get("approved")),
        "groups": reports,
        "training_errors": errors,
    }
    approved_model_files = {model_file for model_file, _ in models}
    # Remove stale approved artifacts when a group no longer passes this run.
    for existing in out_dir.glob("advisor-model-*.json"):
        if existing.is_file() and existing.name not in approved_model_files:
            existing.unlink()

    report_path = out_dir / "advisor-model-approval.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    for model_file, model in models:
        model_path = out_dir / model_file
        model_path.write_text(
            json.dumps(model, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    print(f"REPORT {report_path}")
    print(f"GROUPS_APPROVED {report['groups_approved']}")
    print(f"GROUPS_DISABLED {report['groups_disabled']}")
    return 0 if report["groups_approved"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
