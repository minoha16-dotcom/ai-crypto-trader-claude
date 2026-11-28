#!/usr/bin/env python3
"""Export a fitted sklearn StandardScaler + LogisticRegression pipeline to reviewed JSON.

Run only against a trusted local model file. joblib/pickle loading can execute code.
This script intentionally exports parameters; it does not approve the model.
"""
from __future__ import annotations
import argparse, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
import joblib
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

FEATURES = (
 "dir_ret_1","dir_ret_4","dir_ret_16","log_atr_pct","log_donchian_width_pct",
 "dir_breakout_distance","log_volume_ratio","dir_rsi14","hour_sin","hour_cos",
 "dir_htf_1h_ret_1","dir_htf_1h_ret_4","dir_htf_4h_ret_1","dir_htf_4h_ret_4",
)
def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--model",required=True,type=Path)
    p.add_argument("--dataset",required=True,type=Path)
    p.add_argument("--research-report",required=True,type=Path)
    p.add_argument("--model-version",required=True)
    p.add_argument("--output",required=True,type=Path)
    p.add_argument("--training-cutoff-utc",required=True)
    p.add_argument("--confirm-training-feature-order",required=True,
      help="Must exactly equal the research feature order; prevents exporting a mismatched estimator.")
    args=p.parse_args()
    if args.confirm_training_feature_order != ",".join(FEATURES):
        p.error("confirmed feature order does not exactly match the frozen Direction-Aligned order")
    try:
        report=json.loads(args.research_report.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as exc:
        p.error(f"research report is unreadable: {exc}")
    if report.get("feature_names") != list(FEATURES):
        p.error("research report feature_names do not match the frozen runtime schema")
    if report.get("verdict") != "RESEARCH_DIAGNOSTICS_ONLY_NOT_MODEL_APPROVAL":
        p.error("unexpected research report verdict; export refused")
    if not args.model.is_file() or not args.dataset.is_file() or not args.research_report.is_file():
        p.error("model, dataset and research report must exist")
    # WARNING: only load a model artifact you created and trust.
    fitted=joblib.load(args.model)
    if not isinstance(fitted,Pipeline):
        p.error("expected a fitted sklearn Pipeline; refusing implicit estimator conversion")
    steps=[step for _,step in fitted.steps]
    scalers=[x for x in steps if isinstance(x,StandardScaler)]
    models=[x for x in steps if isinstance(x,LogisticRegression)]
    if len(scalers)!=1 or len(models)!=1:
        p.error("pipeline must contain exactly one StandardScaler and one LogisticRegression")
    scaler,model=scalers[0],models[0]
    if getattr(scaler,"n_features_in_",None)!=len(FEATURES) or getattr(model,"n_features_in_",None)!=len(FEATURES):
        p.error("fitted feature count does not match the frozen 14-feature contract")
    if list(getattr(model,"classes_",[])) != [0,1]:
        p.error("positive class must be encoded as 1 with classes [0, 1]")
    if getattr(model,"coef_",None) is None or model.coef_.shape != (1,len(FEATURES)):
        p.error("expected binary logistic regression coefficient shape (1,14)")
    artifact={
      "schema_version":1,"artifact_status":"SHADOW_ONLY","model_type":"logistic_regression",
      "model_version":args.model_version,"feature_names":list(FEATURES),
      "market_scope":"BINANCE_USDM_PERPETUAL","timeframe":"15m",
      "training_cutoff_utc":args.training_cutoff_utc,
      "dataset_sha256":sha(args.dataset),"research_report_sha256":sha(args.research_report),
      "exported_at_utc":datetime.now(timezone.utc).isoformat(),
      "standard_scaler":{"mean":[float(x) for x in scaler.mean_],"scale":[float(x) for x in scaler.scale_]},
      "logistic_regression":{"coef":[float(x) for x in model.coef_[0]],"intercept":float(model.intercept_[0])},
      "source_pipeline_steps":[name for name,_ in fitted.steps],
      "class_weight":model.class_weight,"random_state":model.random_state,
      "approval":"NOT_APPROVED_RESEARCH_SHADOW_ONLY",
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(artifact,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"output":str(args.output),"artifact_sha256":sha(args.output),
      "dataset_sha256":artifact["dataset_sha256"],"research_report_sha256":artifact["research_report_sha256"],
      "feature_count":len(FEATURES),"status":artifact["approval"]},indent=2))
if __name__=="__main__": main()
