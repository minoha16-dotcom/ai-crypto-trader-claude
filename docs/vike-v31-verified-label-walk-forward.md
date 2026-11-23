# Vike V3.1 — Verified Triple-Barrier Research Pipeline

Status: **fail-closed integration draft; not model approval**

This stage connects the existing Drive-only monthly dataset to the existing
backend/triple_barrier_labels.py engine. It does not recollect data,
modify raw-staging, upload market data, or access the locked
May–October 2026 holdout.

## What the pipeline does

1. Reads the frozen Binance USD-M development window: November 2023–April 2026.
2. Reads the eight frozen symbols and their existing Trade, Mark Price and
   Funding CSVs. Every input file is SHA-256 hashed.
3. Excludes any UTC day without exactly 96 valid, aligned Trade and Mark bars.
   It never fills a gap or joins segments across missing days.
4. Generates direction-neutral Donchian-20 events from closed 15m bars.
   Event generation does not inspect future outcomes.
5. Applies an outcome-independent 48-bar per-symbol overlap cooldown. All
   generated candidates remain in the event manifest with their overlap status.
6. Calls the existing Triple-Barrier labeler only when verified Funding
   coverage, exact Mark alignment and historical liquidation evidence exist.
7. Uses stop=1.5×causal Wilder ATR(14), target=3×ATR, max holding=48 bars,
   next-bar-open entry, stop-first same-bar collision, explicit per-side fees
   and adverse slippage.
8. Runs five expanding chronological walk-forward folds only on verified,
   matured, non-overlapping labels. Each fold has a validation-selected top
   tercile threshold, a 48-bar purge/embargo, pooled Logistic Regression and
   per-symbol diagnostics.
9. Does not run LightGBM automatically. It remains a challenger only after the
   Logistic Regression baseline is valid and a comparison margin is
   pre-registered.

## Funding gate

The existing local audit may show 240/240 structural/reconciliation matches.
That is retained as evidence, but it does not itself change
coverage_status=NOT_VERIFIED. A final-label run requires a reviewed
coverage certificate bound to the exact SHA-256 of every symbol-month Funding
CSV. The certificate schema is:

~~~json
{
  "schema_version": 1,
  "coverage_status": "VERIFIED",
  "evidence_sha256": "<SHA-256 of the exact evidence artifact file>",
  "evidence_artifact_path": "funding-audit-source.pdf",
  "reviewed_by": "<reviewer or review process>",
  "reviewed_at_utc": "<ISO-8601 UTC timestamp>",
  "semantic_basis": "<documented REST/archive field and completeness basis>",
  "symbol_months": [
    {
      "symbol": "BTCUSDT",
      "month": "2023-11",
      "status": "VERIFIED",
      "funding_file_sha256": "<exact SHA-256 of raw-staging CSV>"
    }
  ]
}
~~~

The list must contain exactly one VERIFIED record for every selected
symbol × all 30 development months, and each file hash must match the file
currently on Drive. A status copied from the old 240/240 audit is not enough.
If no acceptable certificate exists, the pipeline records
funding_coverage_not_verified and creates no labels.

## Historical liquidation gate

A current maintenance-bracket schedule is not historical evidence. The
pipeline requires a reviewed manifest with complete time coverage for every
selected symbol and a time-matched isolated, one-way, USDT-linear position
snapshot. The supported manifest shape is:

~~~json
{
  "schema_version": 1,
  "status": "VERIFIED",
  "evidence_sha256": "<SHA-256 of the exact exchange-tier evidence artifact>",
  "evidence_artifact_path": "binance-tier-archive.pdf",
  "reviewed_by": "<reviewer or review process>",
  "reviewed_at_utc": "<ISO-8601 UTC timestamp>",
  "positions": [
    {
      "symbol": "BTCUSDT",
      "valid_from_ms": 1698796800000,
      "valid_to_ms": 1701388800000,
      "margin_mode": "ISOLATED",
      "position_mode": "ONE_WAY",
      "contract_type": "USDT_LINEAR",
      "quantity": 0.001,
      "isolated_wallet_balance": 100.0,
      "no_external_flows": true,
      "snapshot_sha256": "<SHA-256 of the exact position snapshot artifact>",
      "snapshot_artifact_path": "position-snapshots/BTCUSDT-2023-11.json",
      "brackets": [
        {
          "notional_floor": 0.0,
          "notional_cap": 50000.0,
          "maint_margin_ratio": 0.004,
          "cum": 0.0
        }
      ]
    }
  ]
}
~~~

All evidence hashes are now checked against files, not just their
64-character syntax. `evidence_artifact_path` and
`snapshot_artifact_path` must be relative paths located under the directory
containing the certificate/manifest; absolute paths and path traversal are
rejected. The bytes at each path must match the declared SHA-256 exactly.
Keep the evidence files beside or below the manifest directory and preserve
them unchanged. This binds the declared hashes to inspectable local artifacts,
but it does not by itself prove that a public notice is a complete historical
account snapshot or that the current v1 schema correctly models every
maintenance-tier change. The archive inventory document records the remaining
design limitation.

Position intervals must cover the full development period for every selected
symbol. Each event must match exactly one interval. The quantity and isolated
wallet balance are explicit, frozen research-position assumptions for that
interval, not values inferred from a current account. The wallet balance must
reflect available isolated margin after entry fees and before any later funding
settlement. If the position sizing or margin policy changes, create a new
time-bounded interval and evidence record; do not silently reuse today's
settings for earlier years. Cross margin, hedge mode, portfolio offsets,
multi-assets collateral and unsupported contract types remain excluded.

The current Triple-Barrier engine uses one liquidation threshold for the
holding interval. Funding settlements can change isolated wallet balance and
therefore liquidation price. Until dynamic liquidation-margin updates are
implemented, the pipeline conservatively excludes any event with a Funding
settlement during its holding interval using
dynamic_liquidation_margin_after_funding_not_modeled.
This is deliberate; do not remove this guard to increase sample count.

## Walk-forward protocol

Five expanding folds are fixed in code:

| Fold | Validation | Test |
|---|---|---|
| 1 | Nov 2024–Jan 2025 | Feb–Apr 2025 |
| 2 | Feb–Apr 2025 | May–Jul 2025 |
| 3 | May–Jul 2025 | Aug–Oct 2025 |
| 4 | Aug–Oct 2025 | Nov 2025–Jan 2026 |
| 5 | Nov 2025–Jan 2026 | Feb–Apr 2026 |

Training labels must mature before validation; validation labels must mature
before test. A 48-bar embargo is applied at both boundaries. Training is pooled
across eligible symbols from the same venue, contract type and timeframe, while
per-symbol counts and outcomes remain visible. The report requires at least
200 training examples per symbol and 3,000 pooled training examples per fold
before scoring a fold. At least three valid folds are required for WFO
diagnostics.

These WFO metrics are not full statistical acceptance. Synchronized
calendar-time block bootstrap, calibration uncertainty, doubled-cost tests,
legacy baseline comparison and all pre-registered V3.1 gates remain required.
The locked holdout is not opened by this script.

## Run on Windows PowerShell

From the repository root:

~~~powershell
python -m pip install -r requirements-vike-research.txt
python .\scripts\vike_v31_research_pipeline.py --drive-root "G:\Mon Drive\ai trade\Vike V3.1" --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --fee-bps-per-side 5 --slippage-bps-per-side 2
~~~

Optional reviewed evidence files:

~~~powershell
python .\scripts\vike_v31_research_pipeline.py --drive-root "G:\Mon Drive\ai trade\Vike V3.1" --funding-attestation "G:\Mon Drive\ai trade\Vike V3.1\derived\funding-audit\funding-coverage-certificate.json" --liquidation-manifest "G:\Mon Drive\ai trade\Vike V3.1\derived\liquidation-audit\historical-position-brackets.json"
~~~

Do not create these certificates by changing the old audit status manually.
They must be supported by independently reviewed source evidence.

## Outputs

Each run writes only under derived/research-pipeline/<run-id>/:

- research_report.json: verdict, gates, per-symbol diagnostics, sample counts
  and explicit blockers.
- monthly_audits.json: per-symbol/month Trade, Mark and Funding audit.
- excluded_days.json: exact excluded UTC days and reasons.
- event_manifest.csv: every generated event, overlap status, label
  status/reason and causal features.
- walk_forward_results.json: fold results or explicit blocked/inconclusive
  reasons.

If either evidence gate is unresolved, the run still produces event and data
quality diagnostics but does not create Triple-Barrier labels or train a model.
This is an expected safe outcome, not a runtime failure.

## Safety

- raw-staging is read-only.
- No network requests or market-data downloads.
- No Drive upload or deletion.
- No May–October 2026 holdout access.
- No production, paper-execution or live-trading integration.
- No automatic PR merge or Render deployment.
