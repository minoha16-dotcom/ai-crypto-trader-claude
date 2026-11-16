# Advisor model approval — Phase 2.3

## Decision

The selected external source for initial leader discovery is **UnderHyper's
Hyperliquid leaderboard dataset**, combined with Hyperliquid's documented
public Info API for wallet fills. UnderHyper publishes a machine-readable
leaderboard snapshot under CC BY 4.0 with attribution. Its current dataset
contains the top 100 accounts by 30-day volume and the top 100 by 30-day PnL;
it is a discovery cohort, not a statistically representative sample of all
traders. A wallet may be pseudonymous, controlled by a firm, or a market maker.

- Leader discovery and snapshot: https://underhyper.com/data/leaderboard/
- Dataset JSON: https://underhyper.com/data/leaderboard.json
- Official fill history API: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint
- Official rate limits: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits

The official \`userFillsByTime\` request returns at most 2,000 fills per
response and exposes only the 10,000 most recent fills for a wallet. Fill
records include coin, direction, time, price, size, realized closed PnL and
fees. Historical orders are separately limited to the 2,000 most recent
records. These bounds must be recorded; a truncated wallet history is not a
complete training sample. Funding must be collected separately and explicitly
included or excluded in the declared label definition.

## Critical label limitation

Public fills do not reliably disclose the original risk/stop distance for
every completed position. A trader's true R-multiple therefore cannot be
inferred from PnL alone. The dataset-preparation step must declare a
standardized risk unit (for example, the project's 2×ATR or 0.5%-of-entry
distance, whichever is larger), compute net PnL after fees, slippage and
funding according to the fixed configuration, and store that exact definition
in the manifest. Such a label is **system-normalized R**, not the trader's own
reported R. Positions for which entry, full close, fees, funding or candle
features cannot be reconstructed must be excluded and counted by reason.

Do not ingest only winning trades. Select a cohort using a pre-registered
leaderboard snapshot, then retain every reconstructable closed position from
those wallets, including losses and breakeven positions. This avoids training
on winner-only rows. Keep the cohort-selection cutoff strictly earlier than
the first training signal. Historical backfills from a cohort selected using a
later leaderboard snapshot introduce survivor/look-ahead bias and are rejected
by the approval validator.

Binance Futures Copy Trading is useful for visual verification of trader
performance (ROI, PnL, MDD, Sharpe, win rate, profitable closed positions and
total positions), but its public portfolio metrics alone are not individual
trade-level feature/label data. It is therefore not the primary supervised
training source.


## Raw public leader-trade snapshot

The initial raw-data adapter can download the UnderHyper leaderboard JSON and
retrieve each selected wallet's public Hyperliquid fills and funding events:

\`\`\`powershell
python scripts/fetch_hyperliquid_leader_fills.py --cohort top_pnl --start 2026-11-17T00:00:00Z --end 2026-12-17T00:00:00Z --wallet-limit 100 --out artifacts/hyperliquid-raw-leader-data.json
\`\`\`

The collector defaults to the leaderboard's top 30-day PnL cohort, not the
high-volume cohort (which may contain market makers and automated strategies).
It preserves the PnL table's ranking order and rejects unknown leaderboard JSON
layouts rather than scraping arbitrary addresses. The observation window must
start strictly after the leaderboard snapshot;
the adapter rejects retrospective sampling from a cohort selected using later
performance. It uses read-only public endpoints and no account keys. Saturated
time windows are recursively split. A wallet is marked incomplete if any
response cannot be fully retrieved or the documented 10,000 recent-fill
history cap is reached. Incomplete wallets must be excluded from model data.

This raw snapshot is not yet model-ready. A subsequent preparation step must
reconstruct full position lifecycles, join account-level funding, obtain
historical candle features at the opening timestamp, and calculate the
declared system-normalized R label. It must include losses and breakeven trades
from every selected wallet. A raw close PnL by itself is not an R-multiple.

## Frozen per-group dataset contract

The CLI consumes one immutable JSON file with a top-level \`manifest\` and a
\`groups\` array. Each group must be independent:

\`exchange × symbol × market_type × timeframe\`

Example group:

\`\`\`json
{
  "exchange": "hyperliquid",
  "symbol": "BTC/USDC",
  "market_type": "futures",
  "timeframe": "1h",
  "excluded_samples": 12,
  "provenance": {
    "cohort_selection_as_of_utc": "2026-11-01T00:00:00Z",
    "data_start_utc": "2026-11-02T00:00:00Z",
    "data_end_utc": "2026-11-15T00:00:00Z"
  },
  "samples": [
    {
      "sample_id": "wallet-hash-position-id",
      "signal_timestamp": "2026-11-03T10:00:00Z",
      "exit_timestamp": "2026-11-03T14:00:00Z",
      "features": {
        "trend_composite": 0.4,
        "rsi14": 58.0,
        "adx14": 27.0,
        "volume_ratio": 1.3,
        "atr_pct": 0.8
      },
      "r_multiple": 0.75
    }
  ]
}
\`\`\`

Required manifest fields:
- \`source_provider\`, \`source_url\`, \`source_license\`
- \`source_snapshot_utc\`, \`source_snapshot_sha256\`
- \`cohort_selection_as_of_utc\`, \`data_start_utc\`, \`data_end_utc\`
- \`model_version\`, \`feature_schema_version\`, \`code_sha\`
- \`backtest_config\` with \`risk_unit_definition\`, \`fee_model\`,
  \`slippage_model\`, \`slippage_bps\`, \`funding_included\`,
  \`entry_rule\` and \`exit_rule\`

The CLI also hashes the exact dataset file. It records this hash alongside
the source snapshot hash and full backtest configuration. No sample may be
pooled across different exchange, symbol, market-type or timeframe groups.

## Model and acceptance policy

A Ridge regression model (L2 regularization) predicts net system-normalized R
from five features: composite trend, RSI14, ADX14, Volume Ratio and ATR%.
EMA alignment and price-vs-EMA20 must already be combined into the single
composite trend feature. This is a compact baseline model, not a deep network.

Each group is evaluated with expanding chronological walk-forward folds:
training labels must have both signal and exit timestamps strictly before
the next test window. Every fold trains its own model and produces genuinely
out-of-sample R predictions. The final fit is only emitted after all gates
pass.

A group is approved only when all conditions pass:

| Gate | Required |
|---|---:|
| Fully matured training examples in every fold | ≥ 200 |
| Valid chronological walk-forward folds | ≥ 3 |
| Eligible OOS trade signals | ≥ 100 |
| Eligible OOS outcome rows | ≥ 100 |
| Eligible OOS outcomes per fold | ≥ 20 |
| Top-tercile selected outcomes | ≥ 30 |
| Top-tercile mean R | Greater than all-OOS mean R |
| Uplift 95% confidence interval | Lower bound strictly above 0 |
| Source, cohort, dates, hashes and backtest provenance | Complete and valid |

Uplift is \`mean R(top predicted-R tercile) − mean R(all OOS outcomes)\`.
The confidence interval is a deterministic, fold-stratified bootstrap with a
fixed seed. The report also stores the positive-R share and its 95% Wilson
interval. A group is \`DISABLED\` with explicit rejection codes whenever any
gate fails. Approval of BTC/USDC 1h does not approve BTC/USDC 4h or any other
market group.

## Run

\`\`\`powershell
python scripts/train_advisor_model.py --dataset artifacts/advisor-training-dataset.json --out-dir artifacts/advisor-models --code-sha <git-commit-sha>
\`\`\`

The command always writes \`advisor-model-approval.json\`. It writes a
per-group model artifact only for groups marked \`APPROVED\`. An exit code of
2 means no group was approved; this is an intentional fail-closed result,
not a successful model release.

**No live execution is involved.** The output is a research artifact for the
paper-trading Advisor. It must not be loaded by the runtime ranker unless its
group key, feature schema, data/config hashes and approval status all match.
