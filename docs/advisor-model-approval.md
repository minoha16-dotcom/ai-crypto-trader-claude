# Advisor model approval — Phase 2.3

## Data source and selection method

The cohort discovery adapter reads **Hyperliquid's public static leaderboard**
at:

- Raw leaderboard: https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
- UnderHyper dated metadata and attribution: https://underhyper.com/data/leaderboard.json
- UnderHyper methodology: https://underhyper.com/data/leaderboard/
- Public fill/funding Info API: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

The raw leaderboard uses \`leaderboardRows[].ethAddress\` and
\`windowPerformances\`, represented as \`[window, metrics]\` pairs. The collector
ranks the \`month\` PnL value by default, requires positive month volume, and
selects the top 100 active positive-PnL accounts. \`top_volume\` is a separate
explicit cohort; it is never mixed with \`top_pnl\`. The month window is used as
the public 30-day cohort proxy.

These are pseudonymous accounts, not verified individual people. One person
or firm may operate several wallets. A PnL leaderboard is a cohort discovery
signal, not an independently verified record of trading skill or a guarantee
of future results. The top-volume cohort can contain market makers and
automated strategies.

**License boundary:** UnderHyper's metadata page states CC BY 4.0 and is
attributed in the manifest. The raw Hyperliquid leaderboard endpoint does not
state a separate CC BY grant. The collector records both source URLs and hashes
but does not assume UnderHyper's license automatically covers Hyperliquid's
raw data. Review terms before redistributing raw source records.

## Raw data collection

Run the read-only collector with an observation window beginning strictly
after the leaderboard snapshot:

\`\`\`powershell
python scripts/fetch_hyperliquid_leader_fills.py --cohort top_pnl --start 2026-11-17T00:00:00Z --end 2026-12-17T00:00:00Z --wallet-limit 100 --out artifacts/hyperliquid-raw-leader-data.json
\`\`\`

The collector snapshots the exact raw leaderboard bytes, the separately
timestamped UnderHyper metadata, wallet rankings, public fills and funding.
It preserves the raw source SHA-256, metadata SHA-256, observation dates,
endpoint names, page limits and per-wallet completeness flags. Saturated
time ranges are recursively split. It marks a wallet incomplete if a response
cannot be fully retrieved or the documented recent-fill history cap is hit.

Hyperliquid documents that \`userFillsByTime\` returns at most 2,000 fills per
response and exposes only the 10,000 most recent fills for a wallet. The
collector uses a separate 500-row cap for \`userFunding\` range pagination.
A raw snapshot marked complete means the requested Info API ranges were
retrieved within these published limits; it does **not** mean every possible
execution type or the wallet's entire lifetime is represented.

The raw collector always marks wallets \`training_eligible=false\`. Raw fills
are not a ready-made supervised dataset.

## Position reconstruction and label requirements

The adapter output still requires a separate, reviewed preparation step before
training:

1. Reconstruct complete position lifecycles from fills, including adds,
   partial closes, full closes, flips, and any unresolved opening positions.
2. Join funding events to positions and include fees and funding in net PnL.
3. Join fully closed historical candles at the position-entry timestamp and
   calculate the feature vector without using future candles.
4. Declare the system-normalized risk unit (for example,
   \`max(2 × ATR14, 0.5% of entry price)\` multiplied by the appropriate
   position quantity and contract multiplier) and calculate net
   \`r_multiple\`.
5. Exclude and record positions with incomplete history, unresolvable scaling,
   missing entry/exit, liquidation, or unavailable candle features.

Public fills do not reveal each trader's original stop-risk distance reliably.
The computed label is therefore **system-normalized R**, not the trader's own
reported R. Do not use raw realized PnL as an R label.

Do not ingest only winning trades. Retain all reconstructable position signals,
including losses and breakeven outcomes, from the pre-registered selected
cohort. Cohort selection must predate the first training signal. A historical
backfill that uses a cohort selected based on later PnL is look-ahead-biased
and the approval validator rejects it.

## Frozen per-group dataset contract

The training CLI consumes one immutable JSON file containing a top-level
\`manifest\` and a \`groups\` array. Every group is evaluated independently:

\`exchange × symbol × market_type × timeframe\`

Each \`samples\` row must represent one observed position-entry signal and
include:

\`\`\`json
{
  "sample_id": "wallet-hash-position-id",
  "signal_timestamp": "2026-11-18T10:00:00Z",
  "exit_timestamp": "2026-11-18T14:00:00Z",
  "exit_reason": "TARGET",
  "features": {
    "trend_composite": 0.4,
    "rsi14": 58.0,
    "adx14": 27.0,
    "volume_ratio": 1.3,
    "atr_pct": 0.8
  },
  "r_multiple": 0.75
}
\`\`\`

All signal rows, including rows later excluded by exit type, must remain in
the sample file so OOS signal counts and exclusions are not understated.
Only these exit reasons create R training labels:

- Eligible: \`STOP\`, \`STOP_GAP\`, \`TARGET\`, \`TARGET_GAP\`, \`TIME_EXIT\`.
- Excluded: \`PERIOD_END\`, all \`LIQUIDATION*\` reasons, manual/unknown exit
  reasons, or any outcome without a finite R label.

Every excluded reason is counted. Excluded outcomes remain in OOS signal
denominators but do not train the model, contribute to Mean R, or enter the
Top-Tercile/Uplift calculation.

Required manifest fields:

- \`source_provider\`, \`source_url\`, \`source_metadata_url\`, \`source_license\`
- \`source_snapshot_utc\`, \`source_snapshot_sha256\`,
  \`source_metadata_sha256\`
- \`cohort_selection_as_of_utc\`, \`data_start_utc\`, \`data_end_utc\`
- \`model_version\`, \`feature_schema_version\`, \`code_sha\`
- \`backtest_config\`: \`risk_unit_definition\`, \`fee_model\`,
  \`slippage_model\`, \`slippage_bps\`, \`funding_included\`,
  \`entry_rule\`, and \`exit_rule\`

The CLI also hashes the exact frozen training-dataset file. It records this
hash with the source snapshot hashes and complete backtest configuration. No
samples may be pooled across different exchange, symbol, market type or
timeframe groups.

## Model and approval protocol

A compact L2-regularized Ridge regression predicts system-normalized net R
from five features: composite trend, RSI14, ADX14, Volume Ratio and ATR%.
EMA alignment and price-vs-EMA20 must be combined into the single composite
trend feature.

Each group uses expanding chronological walk-forward folds. Training rows
must have both signal and exit timestamps strictly before the next test fold.
The validator independently checks fold IDs, non-overlapping test windows,
matured training labels, per-fold counts and prediction timestamps.

Approval requires every condition:

| Gate | Required |
|---|---:|
| Fully matured eligible training examples in every fold | ≥ 200 |
| Valid chronological walk-forward folds | ≥ 3 |
| OOS position-entry signal observations | ≥ 100 |
| Eligible OOS closed outcomes | ≥ 100 |
| OOS signal observations per fold | ≥ 20 |
| Eligible OOS outcomes per fold | ≥ 20 |
| Selected Top-Tercile outcomes | ≥ 30 |
| Top-Tercile mean R | Greater than all eligible OOS mean R |
| Uplift 95% confidence interval | Lower bound strictly above 0 |
| Source, cohort, dates, hashes and backtest provenance | Complete and valid |

OOS signals and eligible OOS outcomes are separate denominators. A signal
whose outcome is excluded still counts as a signal but does not become an R
label. This is especially important for \`PERIOD_END\`, \`LIQUIDATION\`, and
unknown exits.

Uplift is:

\`mean R(top predicted-R tercile) − mean R(all eligible OOS outcomes)\`.

The confidence interval is a deterministic, fold-stratified bootstrap with
a fixed seed. The report also stores the positive-R share and its 95% Wilson
interval. A group is \`DISABLED\` with explicit rejection codes whenever any
gate fails. Approval of BTC 1h does not approve BTC 4h or any other group.

The final candidate model is fitted on all eligible, matured examples only
after walk-forward validation has been computed. It is exported only if the
approval result is \`APPROVED\`.

## Run

\`\`\`powershell
python scripts/train_advisor_model.py --dataset artifacts/advisor-training-dataset.json --out-dir artifacts/advisor-models --code-sha <git-commit-sha>
\`\`\`

The command always writes \`advisor-model-approval.json\`. A per-group model
file is written only when that group passes every approval gate. Exit code 2
means no group was approved; this is an intentional fail-closed result.

**No live execution is involved.** Approved artifacts are research outputs
for the Paper Trading Advisor. They must not be loaded into the runtime ranker
unless group key, feature schema, source/config hashes and approval status all
match.
