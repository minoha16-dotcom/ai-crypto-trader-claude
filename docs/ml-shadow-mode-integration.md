# Vike ML Shadow Mode Integration

## Status and scope

The backend has an isolated, authenticated diagnostic prediction API and an audit
table. It is deliberately **not connected to the existing scanner or paper-trade
decision path**.

The current Vike research model is scoped to Binance USD-M perpetuals at 15m,
with closed 1h/4h context. The existing AI Crypto Trader scanner supports
KuCoin, Bybit, and OKX for new scans; its EMA/RSI/ADX/ATR/volume signal vector
is not the Vike model feature vector. Do not route those scanner signals into
this model or treat their markets as interchangeable.

Current research remains diagnostic only: selected mean Net R is negative and
LightGBM did not demonstrate stable OOS improvement. Shadow output must not
change a signal, paper trade, position sizing, or any exchange action.

## Implemented backend pieces

- `backend/ml_shadow.py`: JSON artifact validation, exact feature schema,
  direction alignment transform, scaler + logistic probability calculation,
  timestamp checks, and fail-closed behavior.
- `POST /api/ml-shadow/predict`: authenticated diagnostic prediction endpoint.
- `GET /api/ml-shadow/status`: authenticated status endpoint.
- `ml_shadow_predictions`: separate audit table with model/artifact identifiers,
  symbol/side/time, probability, feature hash, and prediction payload.
- `ML_SHADOW_MODE_ENABLED=false` and `ML_SHADOW_MODEL_PATH=` defaults.
- Tests cover schema, finite values, timestamp causality, artifact mismatch,
  direction alignment, disabled mode, and audit isolation from the trades table.

The endpoint accepts raw Vike features and applies the direction-aligned transform
from the research code. It does not fetch market data or calculate raw OHLCV
features itself. The upstream Vike event producer must supply features calculated
from closed candles using the exact research definitions and provide causal
timestamps. These timestamps are checked but are still assertions from the
caller; the API cannot independently prove the source candle data.

## Exact raw feature input

The required `raw_features` keys are:

- `ret_1`, `ret_4`, `ret_16`
- `atr_pct`, `donchian_width_pct`, `channel_position`
- `volume_ratio`, `rsi14` (normalized to [0, 1])
- `hour_sin`, `hour_cos` (UTC)
- `htf_1h_ret_1`, `htf_1h_ret_4`
- `htf_4h_ret_1`, `htf_4h_ret_4`

Direction alignment uses the exact 14 model features:

`dir_ret_1, dir_ret_4, dir_ret_16, log_atr_pct,
log_donchian_width_pct, dir_breakout_distance, log_volume_ratio,
dir_rsi14, hour_sin, hour_cos, dir_htf_1h_ret_1, dir_htf_1h_ret_4,
dir_htf_4h_ret_1, dir_htf_4h_ret_4`.

For SHORT, return and higher-timeframe return features are sign-adjusted,
RSI is mirrored as `1-rsi14`, and breakout distance is measured outward from
the lower Donchian edge. UTC hour features are unchanged.

## Artifact contract

The JSON artifact format is documented in
`docs/ml-shadow-mode-artifact-format.json`. It must contain the fitted
StandardScaler mean/scale and LogisticRegression coefficients/intercept in exact
feature order, plus model version, training cutoff, dataset/report SHA-256,
market scope, and timeframe.

The repository does not currently contain a verified exported fitted model
artifact. The example JSON is a schema template, not a usable model. Never
replace its placeholders with invented values. The current research script
reports diagnostics but did not save a final fitted scaler/model object, so a
separate reviewed export from the Vike research environment is required.

## API request

The authenticated caller sends a JSON body shaped like:

```json
{
  "symbol": "BTCUSDT",
  "side": "LONG",
  "timeframe": "15m",
  "market_scope": "BINANCE_USDM_PERPETUAL",
  "signal_time_ms": 1798762499999,
  "feature_available_time_ms": 1798762499999,
  "htf_1h_close_time_ms": 1798761599999,
  "htf_4h_close_time_ms": 1798754399999,
  "raw_features": {
    "ret_1": 0.001,
    "ret_4": 0.003,
    "ret_16": 0.006,
    "atr_pct": 0.01,
    "donchian_width_pct": 0.03,
    "channel_position": 1.02,
    "volume_ratio": 1.2,
    "rsi14": 0.58,
    "hour_sin": 0.5,
    "hour_cos": 0.866,
    "htf_1h_ret_1": 0.001,
    "htf_1h_ret_4": 0.002,
    "htf_4h_ret_1": 0.004,
    "htf_4h_ret_4": 0.008
  }
}
```

This is a payload-shape example only; its values are not a live signal or a
validated trade. Use closed 15m event time and closed 1h/4h context no later
than the event, with the research timestamp rules.

## Local configuration

```dotenv
ML_SHADOW_MODE_ENABLED=false
ML_SHADOW_MODEL_PATH=/absolute/path/to/reviewed-shadow-model.json
```

Keep the feature disabled until the real artifact and upstream feature producer
are validated. When enabled, the endpoint computes and records a probability
only. It never calls `open_trade`, `close_trade`, risk sizing, or exchange
private/order APIs.

## Remaining release gates

1. Export the final research-fitted LR pipeline as the exact JSON artifact; do
   not export an OOS fold model as if it were a production model.
2. Add/verify a Vike-side live event producer that calculates the raw features
   identically to the training pipeline and submits them to this API.
3. Verify source market candles and HTF timestamp provenance, not just caller
   timestamps.
4. Run the complete test suite and CI on this branch.
5. Deploy only after the artifact and producer exist; keep the flag off in the
   Render environment initially. Enable explicitly for a reviewed shadow period.
6. Keep model selection, thresholds, and trade execution separate. No paper or
   live entry may be controlled by this output until independent positive,
   stable OOS net expectancy and all acceptance gates are satisfied.
