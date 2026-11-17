# Vike historical Hyperliquid cohort and Advisor training

This workflow adds a read-only Vike collector for the historical window
2026-10-16 00:00 UTC (inclusive) through 2026-11-16 00:00 UTC (exclusive).

## Credentials

The collector reads VIKE_API_KEY from the process environment. For local
execution, add it to .env (already excluded by .gitignore). Install dependencies:

~~~powershell
python -m pip install -r requirements-test.txt
~~~

The script loads .env automatically through python-dotenv (included in requirements.txt). GitHub Actions injects
the repository secret of the same name and never prints it.

## Point-in-time cohort

The cohort is selected from the daily Vike positions archive for 2026-10-15,
using rows whose snapshot_ts fall in 23:00–24:00 UTC and whose pnl_cohort is
the stored display label "3x smart". The collector accepts the API slug
"3x_smart" and normalizes it to that archive value. Vike documents this archived label as known at the start of the row's
clock hour and based on fills strictly before that hour. The selection cutoff
is therefore 2026-10-15 23:00 UTC, strictly before the 2026-10-16 observation
start. This is a historical point-in-time label, not a current leaderboard
pasted onto the past.

The snapshot file stores the selected pseudonymous wallet addresses, exact
source file SHA-256, catalogue SHA-256, date range, cohort label/basis, and
collector Git SHA. Wallets are sampled deterministically by a SHA-256 sort when
the cohort contains more than the configured wallet limit.

## Data collection and warm-up

The collector uses Vike's daily fills Parquet archives one UTC day at a time.
Each market-wide raw file is downloaded to a temporary path, filtered to the
selected wallets with Polars, hashed, and deleted before moving to the next day.
No signed archive URL is written to logs or the output manifest.

The default 17-day pre-window warm-up (2026-09-29 00:00 UTC onward) is used only
to reconstruct positions that were already open near the observation start.
The cohort is selected from the daily positions archive, not positions_hourly,
because only the former contains the causal pnl_cohort wallet label. This file
is large; the collector projects only wallet, snapshot_ts and pnl_cohort and
uses Polars streaming execution. The preparer excludes trades whose
entry predates 2026-10-16 from the training sample. A wallet is marked incomplete
and rejected from preparation if its first observed fill for an asset already
has a non-zero start_position, because its original position lifecycle predates
the archived warm-up and cannot be reconstructed safely.

Vike archive fills include each wallet's execution fields, including
start_position, order/trade IDs, fee and fee token. Fill direction is
reconstructed from start_position plus the signed size. Funding, native TWAP
slice executions, and historical order evidence are retrieved separately from
Hyperliquid's public Info API. Duplicate TWAP slice fills that are already in
the Vike archive are removed before position reconstruction. Wallet histories
that reach source caps or contain missing start-position fields are fail-closed.

The existing preparer continues to accept only fully closed, single-entry,
non-scaled linear USDC perpetual positions with known fee/funding evidence and a
recognized trigger exit. Manual, unknown, liquidation, scaled, incomplete, and
unresolved positions remain excluded with explicit reasons. Features use only
candles fully closed before the first opening fill.

## Run locally

In PowerShell, make VIKE_API_KEY available to this process from a secure
password manager or by loading your local .env. Do not print the secret.

~~~powershell
$sha = git rev-parse HEAD

python scripts/fetch_vike_hyperliquid_dataset.py --snapshot-only --start 2026-10-16T00:00:00Z --end 2026-11-16T00:00:00Z --cohort-label "3x_smart" --wallet-limit 1000 --collector-code-sha $sha --out artifacts/vike-cohort-snapshot.json

python scripts/fetch_vike_hyperliquid_dataset.py --snapshot-file artifacts/vike-cohort-snapshot.json --start 2026-10-16T00:00:00Z --end 2026-11-16T00:00:00Z --history-warmup-days 17 --collector-code-sha $sha --out artifacts/hyperliquid-vike-raw-dataset.json

python scripts/prepare_hyperliquid_advisor_dataset.py --raw-dataset artifacts/hyperliquid-vike-raw-dataset.json --out artifacts/advisor-training-dataset.json --code-sha $sha --timeframes 1h,4h

python scripts/train_advisor_model.py --dataset artifacts/advisor-training-dataset.json --out-dir artifacts/advisor-models --code-sha $sha
~~~

The PowerShell example assumes VIKE_API_KEY has already been loaded into the
current process environment. A .env file is not itself read by Windows
PowerShell; the Python script loads it through python-dotenv.

## Run in GitHub Actions

For this open pull request, add the label
run-vike-historical-training to PR #48. The Tests workflow starts the
historical job only for that explicit label event and only from the
feat/vike-hyperliquid-historical-cohort branch; ordinary pushes and test runs
do not start data collection. It reads secrets.VIKE_API_KEY.

The standalone workflow_dispatch entry is also included for use after this
workflow file is present on the repository default branch.

Only the approval report and research-only model JSON files are uploaded as a
short-lived artifact. The workflow deliberately deletes the Vike cohort file,
raw fill dataset, and prepared training dataset before artifact upload; the
repository is public and Vike's current terms restrict redistribution of raw
archives. Keep the raw data locally if needed for a private reproducibility
archive, subject to Vike's terms.

A successful workflow run means collection and validation steps completed. It
does not mean any group was approved. The report lists an explicit acceptance
or rejection for every exchange × symbol × market_type × timeframe group.
Model artifacts remain RESEARCH_ONLY and runtime_eligible=false; the workflow
does not enable them in the live Advisor or execute orders.

## Data-use caveat

Vike states that its historical Hyperliquid data is for personal use under its
Terms of Service and that commercial use requires written agreement. Do not
redistribute raw archives or derived account-level records without reviewing
the current terms and obtaining any required permission.


## Checkpoint and resume behavior

The collector accepts `--state-dir` (default `.vike-checkpoints`) and writes atomic, checksummed checkpoints for each archive day and for each wallet API phase. Checkpoints are bound to the exact cohort snapshot hash, observation dates, warm-up length, selected-wallet digest, and collector code SHA. A changed binding, corrupt checkpoint, or changed cached archive fails closed. Completed API phases are reused, while a new collection run rebuilds the output from the frozen snapshot and verified daily archives, preventing duplicate appends. The final `vike-collection-complete.json` marker is written only after every requested archive day and dataset hash have been verified.

Checkpoint contents and cached Vike archives are local working data. The Actions workflow places them in the runner temporary directory and removes them before artifact upload; it does not cache or upload raw Vike data. GitHub-hosted runners are ephemeral, so cancellation destroys that local state. Cross-run continuation after cancellation requires a private persistent runner or an approved private storage service. None is configured for this repository; therefore a cancelled hosted run can safely restart, but cannot reuse its prior runner-local checkpoint. Do not add Actions cache or artifacts for the raw data to work around this limitation.

Training in the Actions workflow runs only after the completion marker verifies the exact requested 2026-10-16T00:00:00Z–2026-11-16T00:00:00Z window, the dataset hash, and all 48 warm-up and observation archive-day hashes.
