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
