# Vike V3.1 — Frozen Research and Acceptance Protocol

Status: **approved specification; implementation and empirical results pending**  
Scope: Binance USD-M perpetuals, 15m event generation, closed 1h/4h context, pooled multi-asset training, per-asset diagnostics, and conditional Bybit out-of-venue validation. Paper/research only.

This document is the pre-registration contract. Do not change thresholds, event rules, labels, cost assumptions, split dates, or statistical methods after inspecting validation/OOS outcomes. Any pre-test change must be versioned, justified, and recorded before the relevant data partition is opened.

## 1. First vertical slice

- Venue for development/training: Binance only.
- Instrument universe: 5–8 USDⓈ-M perpetual symbols, selected using point-in-time liquidity and listing-history rules, not current survivorship.
- Minimum history: 3 years per included symbol where available.
- Decision timeframe: 15m.
- Context: 1h and 4h bars, only after their close; aggregate from lower timeframe or load independently and verify exact close alignment.
- Models: regularized Logistic Regression first; LightGBM is a challenger, not an assumed upgrade.
- No Spot, COIN-M, delivery futures, other training venues, live orders, or paid workers in this slice.

A symbol lacking the required history, funding coverage, or contract metadata is excluded with an explicit reason. Delisted instruments are retained where historical data is obtainable. The universe selection timestamp and liquidity rule must be frozen before outcome analysis.

## 2. Deterministic, direction-neutral event generator

Use Donchian-20 breakout events on fully closed 15m candles:

- LONG when close[t] > max(high[t-20:t]).
- SHORT when close[t] < min(low[t-20:t]).
- Signal is timestamped at the close of candle t; simulated entry is at the next candle open.
- Both directions use identical logic.
- No EMA/RSI/ADX/volume gate may suppress or create events.
- No repeated event while price remains outside the same prior range: require a fresh re-entry inside the prior channel before another breakout event in that direction.
- All generated events are retained, including losing and untraded events.
- Feature values are computed only from information available at the event timestamp. Higher-timeframe context must be fully closed.
- Generator code, parameters, and event count are hashed and frozen before labels/outcomes are inspected.

This rule generates direction only. The meta-label model makes the take/skip decision.

## 3. Meta-label target and model sequence

There is one learned decision model:

1. The fixed event generator supplies LONG/SHORT direction.
2. Feature engineering describes the market at event time.
3. A binary meta-label indicates whether taking the event under the frozen execution/label policy produces positive net R (positive class) or not (negative class).
4. Logistic Regression is the primary baseline. LightGBM is evaluated only as a pre-registered challenger.
5. Validation selects the probability threshold and Accept/Abstain policy. OOS and final-test data must never select thresholds.

Do not train a separate ML direction model. Do not use execution-cost assumptions, future prices, realized funding, spread, or slippage as model features.

## 4. Frozen Triple-Barrier configurations

Primary configuration:
- ATR: Wilder ATR(14), calculated only through the signal candle.
- Stop: 1.5 × ATR from simulated entry.
- Target: 3.0 × ATR from simulated entry.
- Maximum holding period: 48 complete 15m bars.
- Entry: next bar open, with adverse execution cost.
- Exit: first barrier touched, otherwise time exit at the defined close.
- If SL and TP are both touched within one OHLC bar and intrabar ordering is unavailable, assume SL first.

Sensitivity-only configurations (not eligible for model selection or final acceptance):
- S1: SL 1.0 ATR, TP 2.0 ATR, 32 bars.
- S2: SL 2.0 ATR, TP 4.0 ATR, 64 bars.

Only the primary configuration determines acceptance. Sensitivity results are reported as robustness diagnostics and cannot be used to cherry-pick a passing setup.

## 5. Overlap, portfolio and sample counting

- Count unique generated events separately from executed trades.
- Training sample count is measured after data validation, label maturity, duplicate removal, and overlap control.
- For portfolio simulation, allow at most one open position per symbol. Events occurring while that symbol has an open position are recorded but marked non-executable under the portfolio policy.
- Do not count overlapping events as independent evidence.
- Cross-symbol positions may coexist; their common-time returns remain linked for statistical inference.
- Report both event-level meta-label metrics and portfolio-level net returns. Do not mix these denominators.

## 6. Execution costs and cost-to-R preflight

Costs are simulation assumptions, not input features. Record the source/version and values for:
- taker fee on entry and exit;
- adverse slippage on entry and exit;
- funding paid/received at actual historical settlement timestamps while a position is open;
- any contract-specific execution assumptions.

Do not claim historical spread from OHLCV. Do not invent missing funding or mark-price observations. Missing required Futures data causes the affected interval to fail closed or be excluded with a quantified reason.

For every symbol and label configuration, compute before training:

cost_to_R = (round_trip_fee_pct + round_trip_slippage_pct + funding_cost_pct) / initial_stop_distance_pct

Report median, 90th percentile, and worst observed/assumed cost_to_R, both per symbol and pooled. Preflight policy:
- Primary scenario: median cost_to_R <= 0.25R and 90th percentile <= 0.50R.
- Doubled execution-cost stress (fees and slippage doubled; funding unchanged unless a separately specified funding stress is documented): median cost_to_R <= 0.50R and 90th percentile <= 1.00R.
- If a symbol/configuration fails, it is not silently dropped after looking at PnL; apply the pre-registered liquidity/coverage rule and report the exclusion.

These are explicit research gates, not claims about exchange fee tiers. Fee assumptions must be conservative and frozen before the experiment.

## 7. Data quality and futures requirements

Required Binance USD-M data:
- 15m trade-price OHLCV;
- historical funding rates with settlement timestamps;
- mark-price series sufficient for the declared liquidation/trigger/execution assumptions, preferably mark-price klines;
- instrument metadata and historical listing/delisting availability.

Validator requirements:
- detect timestamp units (ms vs µs) from source/schema and plausible date range; never silently guess;
- normalize internal timestamps to UTC milliseconds;
- validate monotonicity, uniqueness, interval alignment, OHLC consistency, numeric finiteness, and gaps;
- verify published archive checksums where available;
- preserve raw source metadata and hashes;
- report expected vs actual date coverage for every symbol and dataset;
- never forward-fill missing market prices, funding, or mark prices for labels/execution.

Bybit is an external-validation candidate only. Its public Kline and funding endpoints are paginated; this does not prove 3-year coverage for each selected symbol or adequate historical mark-price coverage. Run a coverage probe first. Require overlapping dates and identical instrument/label semantics. If the required coverage is absent, mark out-of-venue validation **UNAVAILABLE**, not PASS. Never train on Bybit outcomes for this experiment.

Three years do not guarantee all market regimes. Produce an ex-ante regime-coverage report by calendar year and volatility/trend buckets. If the history lacks meaningful uptrend, downtrend, and range/high-volatility regimes, qualify the conclusion and do not claim regime-general performance.

## 8. Pooled training and evaluation

- Pool training examples across eligible symbols from the same venue, contract type, and timeframe.
- Preserve symbol identity for diagnostics; do not count symbols/venues as independent replications.
- Aggregate acceptance is based on the pooled, synchronized portfolio OOS outcomes.
- Per-symbol results are mandatory diagnostics (sample count, PF, expectancy, drawdown, calibration, regime coverage), but no individual symbol must independently pass the statistical acceptance test.
- Report leave-one-symbol-out diagnostics to identify dependence on one dominant asset.
- Out-of-venue Bybit results are a separate external validation, not pooled into the Binance training or primary acceptance statistic.

## 9. Temporal split and one-time frozen final test

For a minimum three-year common research window:
- Development/training/rolling walk-forward period: first 30 months.
- Locked final holdout: final 6 calendar months, never used for feature selection, model selection, calibration, threshold selection, cost tuning, or debugging.
- Walk-forward OOS evidence is generated only inside the first 30 months using chronological folds with purging and embargo.
- The final holdout is opened only after the event generator, feature schema, primary barrier, execution assumptions, model artifact, threshold, and analysis code are frozen.

Before opening the final holdout, write an immutable pre-test manifest containing:
- exact UTC boundaries and data hashes;
- code commit SHA and environment/dependency lock;
- event-generator and feature-schema hashes;
- label/cost configuration;
- selected model and fitted artifact hash;
- Validation-derived Accept/Abstain threshold;
- statistical test plan and number of hypotheses;
- one-time evaluation authorization timestamp.

Run the final evaluator once. It must be read-only and emit a signed/hashed report. No tuning or second look is allowed. If code, model, features, thresholds, labels, or costs change after seeing final results, that holdout is consumed; a new future untouched holdout is required.

Require at least 200 executed OOS signals in the aggregate walk-forward evaluation and at least 200 in the final holdout. If either count is not met, verdict is **INCONCLUSIVE**, not PASS and not a reason to relax the threshold.

## 10. Statistical inference and multiple comparisons

Primary inference unit is synchronized calendar time across the full multi-asset portfolio, not individual trades treated as independent.

- Use circular moving-block bootstrap with synchronized blocks across all symbols, preserving cross-asset dependence.
- Primary block length: 7 calendar days (672 × 15m bars); sensitivity block length: 14 calendar days.
- Use at least 10,000 bootstrap replicates and a fixed recorded random seed.
- Report 95% confidence intervals for pooled net expectancy in R and relevant portfolio metrics.
- Do not independently resample assets or trades.
- Correct for the pre-registered model comparisons using Holm-Bonferroni at family-wise alpha 0.05. Record every attempted experiment, including failures.
- The locked final holdout is used once for the selected candidate only; no challenger tournament on the final set.

## 11. Numeric acceptance gates

All primary gates must pass together:

| Metric | Acceptance |
|---|---:|
| Matured, overlap-controlled training examples | >= 3,000 |
| Walk-forward aggregate OOS executed signals | >= 200 |
| Locked final holdout executed signals | >= 200 |
| Pooled OOS Profit Factor | > 1.20 |
| Pooled OOS net expectancy | 95% block-bootstrap CI lower bound > 0 |
| Fold stability | >= 60% of scored test folds have positive net expectancy |
| ECE | <= 0.05 (10 equal-frequency bins; report uncertainty) |
| Brier | Better than validation-fitted prevalence/climatology baseline; positive Brier Skill Score |
| Doubled-cost expectancy | 95% block-bootstrap CI lower bound > 0 |
| Cost-to-R preflight | Meets Section 6 |
| Leakage tests | All pass |
| Out-of-venue | Reported separately; required for expansion claims, unavailable is not PASS |

ECE and Brier are computed on probabilities generated without fitting/calibrating on the scored fold. Calibration is fitted on Validation only. Brier Skill Score is measured against a baseline probability fitted from the corresponding training/validation data, never from the test outcomes.

PF is reported with its denominator and uncertainty; it is not sufficient without the expectancy confidence bound. If a metric is undefined (for example no gross losses), the gate does not automatically pass.

## 12. Required baselines and leakage tests

Baselines:
- take every eligible event;
- current Vike strategy, clearly marked as legacy and evaluated on comparable timestamps/cost assumptions;
- regularized Logistic Regression;
- LightGBM challenger.

Leakage/negative controls:
- randomized labels with time/symbol-aware shuffling; performance must be statistically indistinguishable from no edge;
- feature-shift tests (shift features forward/backward by controlled offsets) and strict timestamp-availability assertions;
- future-data mutation test: modifying candles after event time must not change that event's features or decision;
- duplicate and overlap adversarial tests;
- closed higher-timeframe bar alignment tests;
- purged-label maturity tests.

If LightGBM does not beat Logistic Regression by a pre-registered, statistically supported margin on Validation/WFO, select Logistic Regression. Do not prefer complexity by default.

## 13. Reports and artifacts

Every run must emit:
- dataset_manifest.json
- data_quality_report.json
- cost_to_r_preflight.json
- event_manifest.json
- feature_schema.json
- label_manifest.json
- experiment_registry.json (all trials, including rejected)
- walk_forward_results.json
- calibration_report.json (ECE, Brier, Brier Skill Score)
- synchronized_block_bootstrap.json
- out_of_venue_coverage.json
- frozen_final_test_manifest.json
- verification_report.json

Every report includes exact UTC range, venue, symbol universe, market type, timeframe, code SHA, dataset SHA, config SHA, counts, exclusions, and explicit PASS/FAIL/INCONCLUSIVE reasons.

## 14. Implementation order and release gates

1. Data validator and Binance USD-M coverage probe.
2. Bybit public-data coverage probe (no training use).
3. Cost-to-R preflight.
4. Deterministic event generator.
5. Execution simulator and Triple Barrier.
6. Synchronized portfolio accounting and block bootstrap.
7. Dataset/feature pipeline and leakage tests.
8. Logistic Regression baseline.
9. LightGBM challenger only after baseline pipeline is valid.
10. Walk-forward, calibration, statistical acceptance, and frozen-test evaluator.
11. Paper-only shadow integration after every research gate passes.

No merge to main, no Render deployment, no live trading, and no paid worker are part of this protocol. The current Hyperliquid/Vike wallet cohort collector is not the data source for this Binance USD-M event-based experiment; retain it separately as research tooling and do not mix its wallet labels with market-event labels.
