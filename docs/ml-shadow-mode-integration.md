# ML Shadow Mode Integration — Preflight

## Status

Preflight only. This document does not enable model inference or change the scanner,
paper-trade routes, or deployment configuration.

The current Vike research model is not yet compatible with the repository's
existing scanner. The scanner uses the backend indicator engine (EMA, RSI, ADX,
ATR, and volume ratio), while the Vike experiment uses a separate event/feature
pipeline. A model must never be called with a guessed or partially matched
feature vector.

## Safety boundary

- Shadow mode must be disabled by default.
- Shadow inference may read closed-candle data and write diagnostic records only.
- Shadow inference must not call `open_trade`, `close_trade`, risk sizing, or exchange
  private/order endpoints.
- No model score may change the existing signal, paper-trade eligibility, or
  position sizing.
- Missing, invalid, incompatible, or stale model artifacts must fail closed:
  skip the shadow prediction and record a safe diagnostic reason.
- The currently evaluated models are research-only; selected mean Net R remains
  negative and the LightGBM comparison is not stable OOS. This integration is
  not model approval.

## Required model artifact contract

Before implementing the runtime adapter, export a versioned artifact manifest
that defines:

- model family and artifact format;
- exact ordered feature names and feature-schema version;
- feature transformation/scaling parameters, if any;
- model coefficients/parameters and decision-score method;
- training dataset and label version identifiers;
- training cutoff and evaluation report identifier;
- expected timeframe, market type, venue/instrument assumptions;
- model version and checksum.

The runtime must verify the schema and checksum before inference. Do not use a
pickle/joblib artifact in the production service unless its provenance and
dependency/security implications are explicitly reviewed.

## Planned implementation sequence

1. Add an isolated feature adapter that reproduces the training feature
   definitions using only information available at the prediction timestamp.
2. Add a pure inference module with strict schema, finite-value, and model
   version checks.
3. Add a shadow prediction recorder separate from the signals and trades tables.
4. Add tests for closed-candle timing, feature order, missing values, artifact
   mismatch, disabled-by-default behavior, and proving that shadow execution
   cannot invoke paper-trade write functions.
5. Add a bounded configuration flag, default false, and enable only in a
   non-production test environment first.
6. Run CI and review the draft PR before considering a Render deployment.
7. Deploy only with shadow mode disabled by default; enable it explicitly after
   review and monitor diagnostic predictions without changing trade decisions.

## Acceptance gates

Do not progress to decision-making integration unless the model has a validated
feature adapter, passing leakage/safety tests, reproducible OOS improvement,
and non-negative selected net expectancy under the pre-registered evaluation
protocol. Shadow output is diagnostic only and does not satisfy model approval.
