#!/usr/bin/env python3
"""Train and validate per-market Advisor R-multiple models from a frozen dataset.

Input is a JSON file containing provenance metadata and independent
exchange/symbol/market/timeframe groups. A group model is exported only when
its own chronological OOS validation passes every approval gate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.advisor_model_validation import train_validate_group


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Frozen JSON dataset with manifest and groups")
    parser.add_argument("--out-dir", default="artifacts/advisor-models")
    parser.add_argument("--code-sha", default=None, help="Git commit SHA for provenance")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    raw_bytes = dataset_path.read_bytes()
    dataset_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = payload.get("manifest") or {}
    if args.code_sha:
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
            reports.append({
                "group": group_key,
                "approval": trained["approval"],
                "model_artifact": model_file,
                "walk_forward": {
                    "fold_count": walk_forward["fold_count"],
                    "min_training_samples_per_fold": walk_forward["min_training_samples_per_fold"],
                    "total_training_rows_used_across_folds": walk_forward["total_training_rows_used_across_folds"],
                    "oos_prediction_count": len(walk_forward.get("oos_predictions", [])),
                    "oos_predictions_sha256": hashlib.sha256(predictions_json).hexdigest(),
                    "folds": walk_forward.get("folds", []),
                },
            })
        except Exception as exc:
            reports.append({
                "group": group_key,
                "approval": {
                    "model_status": "DISABLED",
                    "approved": False,
                    "rejection_reasons": [{
                        "code": "TRAINING_OR_VALIDATION_FAILED",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }],
                },
                "model_artifact": None,
                "walk_forward": None,
            })
            errors.append({"group": group_key, "error": f"{type(exc).__name__}: {exc}"})

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
        "source_license": manifest.get("source_license"),
        "source_snapshot_utc": manifest.get("source_snapshot_utc"),
        "code_sha": manifest.get("code_sha"),
        "groups_requested": len(groups),
        "groups_approved": sum(1 for row in reports if (row.get("approval") or {}).get("approved")),
        "groups_disabled": sum(1 for row in reports if not (row.get("approval") or {}).get("approved")),
        "groups": reports,
        "training_errors": errors,
    }
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
