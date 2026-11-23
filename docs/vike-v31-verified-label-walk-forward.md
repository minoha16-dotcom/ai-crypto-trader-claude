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

## Historical liquidation gate — schema v3

The liquidation gate now requires a complete, hash-bound history of Binance
USD-M maintenance tiers, but it no longer requires fabricated historical
user-account snapshots. Position quantity and isolated wallet balance are
derived independently for every event from a frozen synthetic research account.

The v3 manifest has this shape:

~~~json
{
  "schema_version": 3,
  "status": "VERIFIED",
  "reviewed_by": "<independent reviewer>",
  "reviewed_at_utc": "<ISO-8601 UTC timestamp>",
  "research_account": {
    "initial_equity_usdt": 10000,
    "risk_fraction": 0.005,
    "max_leverage": 3,
    "margin_mode": "ISOLATED",
    "position_mode": "ONE_WAY",
    "contract_type": "USDT_LINEAR",
    "account_scope": "INDEPENDENT_EVENT_RESET_NO_CONCURRENT_POSITIONS"
  },
  "maintenance_bracket_history": [
    {
      "symbol": "BTCUSDT",
      "valid_from_ms": 1698796800000,
      "valid_to_ms": 1701388800000,
      "applicability": "NOT_AFFECTED",
      "source_url": "https://www.binance.com/en/support/announcement/detail/<id>",
      "source_artifact_path": "exchange-notices/BTCUSDT-tier-update.html",
      "source_sha256": "<SHA-256 of exact downloaded source artifact>",
      "brackets": [
        {
          "notional_floor": 0,
          "notional_cap": 50000,
          "maint_margin_ratio": 0.004,
          "cum": 0
        }
      ]
    }
  ]
}
~~~

The synthetic account is fixed at 10,000 USDT, 0.5% risk per event (50 USDT),
and a maximum 3x notional cap. Quantity is derived from the stop loss after
adverse entry/stop slippage and both-side taker fees:

- risk budget = equity × risk fraction;
- per-unit stop loss = adverse entry-to-stop fill loss + entry/exit fees;
- quantity = minimum of (risk budget / per-unit stop loss) and
  (equity × maximum leverage / entry fill);
- initial isolated margin = position notional / 3;
- isolated wallet balance = initial margin less the entry-fee reserve.

Each event resets to the same 10,000 USDT equity snapshot. This is a
single-event research convention, not a portfolio equity curve: it does not
model concurrent positions, capital contention, compounding, funding transfers
to margin, or historical user balances. Schema v2 historical account snapshots
are rejected rather than silently reinterpreted.

The loader requires all selected symbols to have uninterrupted
maintenance-bracket coverage across the complete development window, rejects
gaps/overlaps, verifies each source artifact against its SHA-256, and preserves
the announcement applicability field. A public announcement does not by itself
prove the tier table was effective for every position mode or account class;
reviewers must verify contract type, effective time, complete tiers, and
whether existing positions were affected. Do not mark an interval VERIFIED
from search snippets or a current leverage-bracket API response.

Funding settlements during an open position remain excluded until the engine
updates isolated wallet balance and liquidation price after each settlement.
This conservative limitation remains in force.

## Funding archive review packet

Run the read-only audit on the existing Drive archive:

~~~powershell
python .\\scripts\\vike_v31_funding_archive_audit.py --drive-root "G:\\Mon Drive\\ai trade\\Vike V3.1"
~~~

It writes derived/funding-audit/funding-archive-evidence.json and
funding-coverage-certificate.template.json. The packet binds SHA-256 hashes
for all 240 symbol-month Funding CSVs and reports row counts, timestamp order,
duplicates, timestamp ranges, mark-price/rate ranges, and observed interval
histograms. It does not alter raw-staging.

The generated template deliberately remains PENDING_REVIEW and is rejected
by the labeling gate. Structural integrity is not proof of complete exchange
history. A reviewer must reconcile the archived settlements with an
authoritative Binance retrieval/archive record, including pagination and
contract-specific interval changes, then create the reviewed certificate with
the exact 240 current file hashes. Never promote the template by changing only
its status field.

Binance documents GET /fapi/v1/fundingRate as an ascending historical funding
endpoint with inclusive millisecond bounds and a maximum page size of 1,000;
each record includes funding time, rate, and associated mark price. The API's
paging semantics mean an auditable retrieval log must record symbol, requested
start/end, page boundaries, response counts, and raw response hashes. See the
[USDⓈ-M Funding Rate History API](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History).
Current endpoint output alone cannot establish historical completeness without
a full paginated archive/retrieval record.

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
