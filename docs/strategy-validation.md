# Strategy validation design

Status: the v1 chronological walk-forward runner and the pre-registered research CLI are implemented. A run produces an evidence report, but results are not approved until dataset coverage, trade counts, all out-of-sample folds, and ablations are independently reviewed.


## 0. Implemented Walk-Forward Runner (v1)

The backend exposes `GET /api/walk-forward`; the private MCP bridge exposes
the same operation as `run_walk_forward`. It supports rolling and expanding
windows, train/validation/test sizes, a configurable step, warm-up history,
Volume Ratio override, timestamp continuity checks, and a cap of 50 folds.

The runner's boundary policy is explicit and enforced:
- `max_holding_bars` defaults to 20. A position is exited at the close of
  the bar that reaches this maximum, unless its stop or target exits first.
- `purge_bars` must be at least `max_holding_bars`; its default is 20.
- `embargo_bars` defaults to the greater of `warmup_bars` and
  `max_holding_bars` (50 bars with the default warm-up of 50). A smaller
  explicit value is rejected.
- Every train, validation, and test simulation is independent and starts with
  a fresh balance. Any remaining position is force-closed at that period's
  final candle close with normal modeled fees and slippage
  (`exit_reason=PERIOD_END`). No open mark-to-market P&L is carried into
  another segment or included as unrealized period return.
- The aggregate reports winning, losing, and breakeven trades separately.
  Test windows must not overlap (`step_bars >= test_bars`).

Each period receives the immediately preceding warm-up candles as indicator
context, but those candles are not scored. The API excludes the final forming
exchange candle, rejects incomplete requested history, and validates candle
timestamps and configured gap tolerance.

The maximum holding limit is an explicit **walk-forward evaluation assumption**;
it does not alter the ordinary `GET /api/backtest` endpoint's default behavior.
The forced period-end exit prevents trades from remaining open across split
boundaries, but it is still a bar-based market-exit assumption. Purge and
embargo constraints bound the configured feature lookback and holding horizon;
they are safeguards, not a formal proof against every possible source of data
leakage.

Important limitation: v1 evaluates the current strategy and risk settings as
fixed, pre-registered rules. It reports train and validation diagnostics and
forward-test metrics, but it does not fit a model, search parameters, or select
a threshold using validation. Automated parameter selection and candidate
comparison must be implemented as a separate, carefully tested stage before
describing this as an optimized strategy.

The result contains the index and timestamp boundaries for every period,
per-period metrics, aggregate test metrics, and explicit limitations. Period
balances reset independently, so the sum of fold P&L is not a continuous
portfolio equity curve.

Example:

```text
GET /api/walk-forward?exchange=kucoin&symbol=BTC/USDT&market_type=spot&timeframe=1h&limit=1500&train_bars=250&validation_bars=100&test_bars=100&step_bars=100&warmup_bars=50&purge_bars=20&embargo_bars=50&max_holding_bars=20&window_mode=rolling&max_folds=10
```

## 1. Objective and non-goals

Build a repeatable evaluation process that estimates whether a rule set
generalizes to unseen time periods. This is a research process, not a forecast,
trade recommendation, or proof of profitability. It must not place exchange
orders or use private account endpoints.

The evaluation must preserve the current engine's conservative assumptions:
signals use closed candles, entries occur at the next candle open, stop gaps
fill at the adverse open, same-candle stop/target ambiguity resolves to the
stop first, and costs are included. Funding, margin, liquidation, order-book
depth, and realistic execution remain limitations until explicitly modeled.

## 2. Walk-forward evaluation protocol

1. Freeze the strategy definition, candidate parameters, instruments,
   timeframes, fee/slippage assumptions, and metrics before viewing the final
   out-of-sample results.
2. Sort candles by exchange timestamp. Never shuffle observations and never
   use a candle before it has closed.
3. Use expanding or rolling chronological windows. Within each fold, use an
   earlier training segment for candidate exploration, a later validation
   segment for selecting one configuration, and the immediately following
   test segment only once for out-of-sample evaluation.
4. Advance the window forward and repeat. Report every fold separately as well
   as the combined out-of-sample result; do not report only the best fold.
5. Keep indicator warm-up candles available at the start of a segment, but
   do not score trades whose signal or entry depends on information from the
   future segment. Exclude the forming final candle.
6. Bound the trade-label horizon and prevent positions from crossing a
   split: configure a maximum holding horizon, purge at least that many bars,
   and force-close any remaining position at the period's final candle close.
   Apply an embargo at least as long as the greater of the feature lookback
   and maximum holding horizon. These are explicit safeguards, not a proof
   against every possible source of data leakage.
7. Do not tune on the final test fold. If a configuration is changed after
   inspecting an out-of-sample result, that period becomes development data
   and a new untouched test period is required.

The first implementation should make window sizes, step size, rolling versus
expanding mode, warm-up length, purge/embargo length, and minimum observations
explicit inputs. It should fail closed when a fold has insufficient candles or
contains timestamp gaps beyond the configured policy.

## 3. Volume Ratio experiment: 0.8 / 1.0 / 1.2

The scanner compares the last closed candle's volume with the mean volume of
the preceding 20 closed candles. The engine's current default threshold is
1.0. The backtest API and MCP run_backtest tool now accept an optional
volume_ratio_threshold override for controlled experiments; omitting it
uses the configured default.

Evaluate exactly these pre-registered thresholds:

- 0.8: permissive volume filter.
- 1.0: current default baseline.
- 1.2: stricter volume filter.

For each threshold, use identical closed-candle datasets, folds, symbols,
market types, timeframes, initial balance, risk settings, fees, and slippage.
Change only the threshold. Report, per fold and in aggregate:

- qualifying signals and executed simulated trades;
- filter rejection counts and skipped-trade reasons;
- net return, net P&L, expectancy in R, profit factor, and win rate;
- maximum drawdown, exposure, holding duration, and turnover;
- sample size and the number of symbols/timeframes represented.

A threshold that produces fewer trades is not automatically better. Compare
risk-adjusted out-of-sample results and fold-to-fold stability. Do not select
a threshold from in-sample profitability alone. Mark the result inconclusive
when the sample is too small or market coverage is narrow.

## 4. Correlated-indicator impact

The live engine currently calculates EMA 20, EMA 50, RSI 14, ATR 14, ADX 14,
ATR percent, and Volume Ratio. The directional score gives separate votes to
EMA 20 versus EMA 50 and close versus EMA 20; those two trend features can be
correlated. ADX, ATR percent, and Volume Ratio act as filters rather than
additional directional votes.

Measure redundancy using only training data in each fold:

1. Save a timestamped feature matrix from the exact closed candles used for
   each decision. Keep the current engine's feature definitions unchanged.
2. Calculate Pearson and Spearman correlations for continuous features; for
   binary or categorical signals, use suitable association measures and state
   the method.
3. Flag highly correlated pairs for investigation (for example, absolute
   correlation at or above 0.80); this is a review threshold, not proof that
   either feature is useless.
4. Run controlled ablations: baseline; remove one feature vote; group the
   correlated trend votes into one contribution; then compare the same
   configurations on the untouched forward test segment.
5. Compare changes in trade count, expectancy, profit factor, drawdown, and
   fold stability. A feature is retained only when it contributes repeatable
   out-of-sample information, not merely because it improves the training
   score.

Do not add MACD or EMA 200 to engine-driven claims until they are implemented,
tested, and exposed in the backend feature payload. Skills may discuss them as
optional external chart indicators only when the source data is supplied.

## 5. Executable research runner and feature ablations

Run the pre-registered public-data experiment from the repository root:

```powershell
$env:STRATEGY_VALIDATION_EXCHANGES = "kucoin,okx"
$env:STRATEGY_VALIDATION_LIMIT = "5000"
python scripts/run_strategy_validation.py
```

The default universe is Spot BTC/USDT and ETH/USDT plus Futures BTC/USDT
for each selected exchange. The run uses 1h candles, requires at least 600
closed candles, removes the still-forming candle, and rejects timestamps with
missing bars rather than filling them. The default exchanges are KuCoin and
OKX; Bybit is intentionally not included in the default research run while its
public connectivity is unverified from the target network.

Pre-registered comparisons:
- Volume Ratio thresholds 0.8, 1.0, and 1.2 with the baseline signal votes.
- Baseline directional votes.
- Ablation without the EMA20/EMA50 alignment vote.
- Ablation without the close/EMA20 vote.
- A single composite trend vote replacing the two correlated trend votes.

The report includes all per-fold results, total out-of-sample trade count,
positive-fold share, expectancy in R, profit factor, worst fold drawdown, and a
long-only buy-and-hold reference net of estimated round-trip fees/slippage.
Spot and Futures datasets are reported separately. Futures use the existing
simplified 2x isolated paper model and its fixed funding/maintenance/liquidation
assumptions; do not interpret those Futures results as venue-matched execution.

The report's `evidence_status` is deliberately conservative:
- `INCONCLUSIVE_INSUFFICIENT_COVERAGE_OR_TRADES` when the report lacks
  four complete datasets, twelve baseline test folds, or 100 baseline
  out-of-sample trades.
- `POSITIVE_OUT_OF_SAMPLE_SIGNAL_REQUIRES_INDEPENDENT_REVIEW` only when the
  minimum evidence is present, the average baseline fold return is positive,
  at least 55% of baseline test folds are positive, and mean return exceeds
  the buy-and-hold reference.
- `NO_ROBUST_POSITIVE_EDGE_DEMONSTRATED` otherwise.

These labels are research triage, not proof of profitability. The report is
not a fully risk-matched benchmark, does not apply multiple-testing correction,
and does not claim independent samples across overlapping training windows.
Any strategy change after reading the report requires a new untouched test
period. GitHub Actions stores JSON and Markdown outputs as a workflow artifact.

## 6. Skill, MCP, and backend responsibilities

- **Backend** is authoritative for exchange market data, indicator calculations,
  risk checks, paper-trade state, and the implemented single-window backtest.
- **Claude Skills/plugin** provide reusable analysis instructions and workflows.
  They do not independently fetch data, change backend settings, or execute
  trades.
- **MCP server** exposes selected backend capabilities as tools to an
  MCP-compatible client (including the private ChatGPT integration). It is a
  tool bridge, not a skill runtime and not a second strategy engine.
- A connected client may use MCP market-data, scan, risk, journal, and
  backtest tools while following its own skill instructions. The repository
  does not claim automatic synchronization or execution of Claude Skills
  inside ChatGPT.

A Trader Profile is intentionally not introduced in this phase: no current
product requirement or persisted profile schema defines its fields, consent,
or effect on the risk engine. It should be added only after a product decision
defines these behaviors. It must never silently override central risk limits.

## 7. Report and acceptance criteria

Each research report should record code version, data source, exchange, symbol,
market type, timeframe, date range, candle count, gap/shortfall metadata,
window definitions, selected parameters, fees, slippage, funding treatment,
and all metric definitions.

A completed walk-forward implementation is acceptable only when it has tests
for chronological ordering, warm-up handling, boundary purging, no future
feature leakage, insufficient-fold rejection, reproducible fold outputs, and
consistent fee/risk assumptions with the existing paper engine.

No threshold or strategy should be labelled validated until all pre-registered
folds and ablations have been run and their out-of-sample results reviewed.
