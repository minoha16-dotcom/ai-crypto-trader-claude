# Binance BTCUSDT November 2023 Pilot — GitHub Preflight

## Purpose

This manual GitHub Actions workflow downloads the official public Binance USD-M archives for:

- Symbol: `BTCUSDT`
- Market: USD-M Futures
- Interval: `15m`
- Window: `2023-11-01T00:00:00Z` through (exclusive) `2023-12-01T00:00:00Z`
- Trade klines: one monthly archive
- Mark price klines: 30 daily archives
- Funding: public `/fapi/v1/fundingRate` history for the same window

Every ZIP is checked with its official `.CHECKSUM` sidecar and ZIP CRC. The report audits timestamp coverage, duplicates, ordering, missing/extra bars, alignment, and funding response integrity. The known local-data observation is one missing mark-price bar at `2023-11-10T03:45:00Z`; the workflow independently rechecks the official source.

## Run the preflight

The workflow file currently exists only on the PR #51 feature branch, not on the repository's default `main` branch. GitHub requires a `workflow_dispatch` workflow to exist on the default branch before its **Run workflow** control or API dispatch is available. Therefore, do not expect this workflow to be manually runnable yet.

To make it runnable, one of these repository changes must be explicitly approved:
- add a minimal, inert workflow-registration file with the same path to `main`, then dispatch the feature-branch version; or
- merge the relevant PR changes to the default branch.

Neither action has been taken. PR #51 remains Draft and unmerged, and `main` is unchanged.

Once registered on the default branch, the run steps are:
1. Open the repository's **Actions** tab.
2. Select **Binance BTCUSDT November 2023 Data Preflight**.
3. Select branch `feat/vike-v31-acceptance-protocol`.
4. Click **Run workflow**.
5. Open the completed run and download artifact `binance-btcusdt-nov2023-preflight`.

The workflow is manual-only. It does not create paid workers, access private credentials, place orders, merge a PR, or deploy Render. Raw ZIP files and the funding response are removed from the runner workspace after the report is uploaded. Only the data-quality JSON report is retained as an artifact for 30 days.

## Important: this is not the training workflow

The current Advisor approval implementation is specifically scoped to external-wallet position outcomes. Its required provenance includes a point-in-time external cohort and must not be filled with invented Binance values. The current Futures backtest also does not yet apply the historical funding-rate schedule and synchronized mark-price OHLC to liquidation checks.

For those reasons, this workflow is deliberately a **data preflight only**. Its report states `training_started: false` and `training_gate.status: BLOCKED_NOT_READY`. It does not create labels, a model dataset, or an approved model artifact.

Before a Binance Advisor training run can be considered valid, the project still needs:

1. A Binance-specific causal sample-preparation path with explicit signal and exit timestamps.
2. Historical funding events applied at their actual funding timestamps and mark-price-based liquidation checks.
3. A distinct research-only approval scope for project-generated market-event/backtest outcomes, without weakening the existing external-cohort policy.
4. Actual eligible sample counts sufficient for the existing policy: at least 200 fully matured training labels per fold, 3 chronological folds, and at least 100 OOS signals and eligible outcomes, plus positive top-tercile uplift with a confidence interval whose lower bound is above zero.
5. Leakage, missing-bar, gap, and deterministic-reproduction tests.

A single month and one symbol may not produce enough independent samples to satisfy these gates. If it does not, extend the historical window or research universe under a pre-registered protocol; do not lower the gates or fabricate samples.

## Data integrity rules

- Never forward-fill or synthesize the missing mark-price candle.
- Never mix Binance market-event outcomes with Hyperliquid external-wallet outcomes.
- Keep the original raw archives unchanged.
- Do not treat a current exchange instrument limit as a historical November 2023 limit.
- A successful download or boundary probe is not evidence of a profitable strategy or an approved model.

## Local Vike V3.1 engineering pilot (not approval)

A separate research-only pilot runner is available at
`scripts/binance_nov2023_v31_pilot.py`. Unlike the preflight workflow above,
it reads the local raw archives, generates Donchian-20 events, calculates
causal features, applies the primary 1.5 ATR / 3 ATR / 48-bar Triple Barrier,
subtracts explicit fee/slippage assumptions and actual funding settlements,
and writes a chronological diagnostic Logistic Regression experiment.

Run from the repository root in PowerShell (Python 3.11 is supported):

```powershell
python -m pytest tests/test_binance_nov2023_v31_pilot.py -q

python scripts/binance_nov2023_v31_pilot.py `
  --trade-zip "C:\Vike_Data\binance-nov2023-preflight\BTCUSDT-15m-2023-11.zip" `
  --mark-dir "C:\Vike_Data\binance-nov2023-preflight" `
  --funding-json "C:\Vike_Data\binance-nov2023-preflight\funding-rates.json" `
  --out-dir "C:\Vike_Data\binance-nov2023-v31-experiment" `
  --fee-bps-per-side 5 `
  --slippage-bps-per-side 2
```

The 5 bps taker fee and 2 bps adverse slippage per side are explicit pilot
assumptions, not assertions about the user's historical fee tier or measured
historical spread. Change them only before interpreting the pilot.

Outputs:
- `dataset_manifest.json`: source hashes, window, event, label, feature and cost configuration.
- `events.json`: generated events, features, label outcomes and exclusion reasons.
- `pilot_report.json`: data quality, counts, exclusions, cost-to-R, net-R diagnostics,
  training status and explicit protocol-gate failures.

The runner accepts the known missing mark-price candle as a data-quality gap
rather than silently filling it. Any event whose holding interval crosses a
missing mark-price bar is excluded with `MISSING_MARK_PRICE_DURING_HOLD`.
Trade-price candles must cover the full registered November window without gaps.

The model fit uses a deterministic chronological 70/30 diagnostic split.
This is only to test end-to-end mechanics on one month and one symbol. It is
not the protocol's expanding walk-forward evaluation, locked six-month holdout,
synchronized block bootstrap, or model-approval process. The report always
marks `runtime_eligible=false`; no model is exported for runtime use.
One-month results must not be described as evidence of edge or Vike V3.1 acceptance.
