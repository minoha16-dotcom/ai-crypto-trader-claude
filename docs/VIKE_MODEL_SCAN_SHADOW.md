# Vike Model Scan (Shadow Only)

The dashboard's **Run Vike AI scan** uses the frozen Vike V3.1 research scope:
- Binance USD-M perpetual public klines only
- 15m Donchian-20 breakout events
- closed 1h and 4h context
- frozen symbols: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT
- direction-aligned 14-feature Logistic Regression inference
- current scan top-tercile display only; this is not a calibrated trade threshold or model approval

The live feature code mirrors the research event definitions and fails closed on forming candles, stale/future higher-timeframe context, invalid feature values, unknown symbols, or missing model artifact. Predictions are appended to the existing `ml_shadow_predictions` audit table. The route has no paper-trade or exchange-order calls.

## Export the fitted model locally

Only load a model artifact that you created and trust: joblib/pickle loading can execute code.

Run from the Vike repository environment after confirming the saved pipeline and source report:
```powershell
python .\scripts\export_vike_lr_shadow.py `
  --model "PATH_TO_TRUSTED_FITTED_PIPELINE.joblib" `
  --dataset "PATH_TO_DEVELOPMENT_LABELS.csv" `
  --research-report "PATH_TO_DIRECTION_ALIGNED_REPORT.json" `
  --model-version "vike-lr-shadow-<run-id>" `
  --training-cutoff-utc "2026-05-01T00:00:00Z" `
  --confirm-training-feature-order "dir_ret_1,dir_ret_4,dir_ret_16,log_atr_pct,log_donchian_width_pct,dir_breakout_distance,log_volume_ratio,dir_rsi14,hour_sin,hour_cos,dir_htf_1h_ret_1,dir_htf_1h_ret_4,dir_htf_4h_ret_1,dir_htf_4h_ret_4" `
  --output ".\artifacts\vike_lr_shadow.json"
```

The exporter verifies the report feature order, the exact `StandardScaler -> LogisticRegression` pipeline, the 14-dimensional fitted scaler/model, binary classes `[0,1]`, `class_weight=None` (matching the Direction-Aligned baseline), and records dataset/report SHA-256. A provisional class-weighted model is rejected. It deliberately marks the artifact `SHADOW_ONLY` and `NOT_APPROVED_RESEARCH_SHADOW_ONLY`. It does not turn a failed research result into an approved model. Do not use a placeholder/test model.

## Configure the Render service only after reviewing the exported JSON

1. Upload the JSON artifact to a private, access-controlled path available to the service (do not commit model data or credentials to Git).
2. Set `ML_SHADOW_MODEL_PATH` to that path.
3. Set `ML_SHADOW_MODE_ENABLED=true` only after independently checking artifact provenance, training feature order, and live feature calculations.
4. Manually deploy because Render Auto Deploy is intentionally disabled.
5. Test the authenticated `/api/ml-shadow/status`, then the dashboard scan and `/api/ml-shadow/predictions`. Keep all outputs diagnostic.

The existing research results do not meet model-approval gates: OOS uplift confidence interval includes zero, selected net expectancy was negative, and funding/maintenance/liquidation provenance is not fully verified. Therefore top-tercile ranking is an exploratory ordering of the current candidates only. It must not feed scanner signals, risk sizing, paper-trade opening, or exchange orders.
