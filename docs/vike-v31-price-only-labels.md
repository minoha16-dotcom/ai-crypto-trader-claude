# Vike V3.1 Price-only and Funding-aware Labels

## Purpose

This is a separate research-label path that can run without historical Maintenance Bracket evidence. It does **not** approve or alter the Maintenance Bracket gate.

The script prepares labels only. It does not fit Logistic Regression, LightGBM, or any other model.

## Frozen scope

- Venue: Binance USD-M USDT perpetual futures.
- Symbols: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT.
- Development data: November 2023 through April 2026 inclusive.
- Holdout: May through October 2026; never loaded or accessed.
- Event generator: existing direction-neutral Donchian-20 generator on closed 15m candles.
- Overlap: fixed 48-bar per-symbol cooldown, applied without looking at outcomes.
- Entry: next 15m trade-candle open.
- Stop: 1.5 ATR.
- Target: 3 ATR.
- Maximum holding period: 48 15m bars.
- Barrier trigger source: aligned Mark Price OHLC.
- Collision rule: stop-first if stop and target are both touched in one 15m bar.
- Stop gaps: use Mark Price bar open when it has gapped through the stop.
- Time exit: 48th Mark Price candle close.
- Costs: 5 bps taker fee per side and 2 bps adverse slippage per side by default.
- Outcome: \`net_r\`; \`label=1\` when \`net_r > 0\`, otherwise \`0\`.

## Funding policy

Funding is not included as a cashflow in this label definition. The verified Funding CSV is used only to identify settlement timestamps. Any event with a Funding settlement from entry time through the exit candle close is excluded. The exit-candle-close boundary is conservative because 15m OHLC does not reveal the exact intrabar exit timestamp.

The script verifies the local Funding CSV SHA-256 against the AI-reviewed Funding attestation for all 240 symbol-month groups. The attestation and \`funding-archive-evidence.json\` must be in the same directory because the attestation binds the evidence file by relative path and SHA-256.

## Explicit non-scope

- No Maintenance Bracket table or status is loaded or changed.
- No leverage, position sizing, isolated margin, maintenance margin, liquidation price, or liquidation breach is modeled.
- No Funding cashflow is added to \`net_r\`.
- No model training or model acceptance is performed.
- No raw-staging file is changed.
- No network access or data download occurs.

These labels must not be described as a leveraged-futures PnL or liquidation-aware backtest. They are price-only research outcomes with funding-crossing events excluded.

## PowerShell run instructions

First, place these two files together in a review directory under \`derived\`:

- \`vike_v31_funding_coverage_attestation_ai_reviewed.json\`
- \`funding-archive-evidence.json\`

For example:

\`\`\`powershell
$driveRoot = "G:\Mon Drive\ai trade\Vike V3.1"
$evidenceDir = Join-Path $driveRoot "derived\funding-audit\ai-review"
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null

# Copy the two reviewed evidence files from the extracted review package.
Copy-Item ".\vike_v31_funding_coverage_attestation_ai_reviewed.json" $evidenceDir
Copy-Item ".\funding-archive-evidence.json" $evidenceDir

python .\scripts\vike_v31_price_only_labels.py \`
  --drive-root $driveRoot \`
  --funding-attestation (Join-Path $evidenceDir "vike_v31_funding_coverage_attestation_ai_reviewed.json")
\`\`\`

Outputs are written only to:

\`G:\Mon Drive\ai trade\Vike V3.1\derived\price-only-labels\<run_id>\`

Expected artifacts:

- \`labels.csv\` — all generated candidates, labels and exclusion reasons.
- \`research_report.json\` — scope, counts, per-symbol outcomes and explicit limitations.
- \`monthly_audits.json\` — source row counts and Funding hash checks.
- \`excluded_days.json\` — incomplete price days.
- \`dataset_manifest.json\` — source file hashes and run configuration.

## Interpretation

A successful run means the price-only label dataset was prepared. It does not mean the strategy is profitable, the model is accepted, the Maintenance Bracket gate is closed, or the system is ready for live trading.


## Funding-aware research mode (new)

The default mode remains the legacy Price-only benchmark: Funding settlements during a position are excluded. Use `--funding-aware` to apply every settlement cashflow per base unit for Binance USD-M linear USDT perpetuals:

`funding_cashflow_per_unit = -side_sign * settlement_mark_price * funding_rate`

where `side_sign=+1` for LONG and `-1` for SHORT. Positive rates debit LONG and credit SHORT. Funding cashflows are included in net PnL after fees and adverse slippage. Time exits remain eligible; settlements from entry time through the 48th Mark Price candle close are included conservatively because 15m OHLC does not identify the intrabar exit instant.

Funding CSV hashes are checked against the reviewed attestation, but independent retrieval completeness/provenance remains unverified and is reported as a blocker. This mode does not model maintenance margin, leverage, isolated wallet evolution, or liquidation, and it is not a liquidation-aware futures backtest.

## Feature timestamp provenance

The event generator records `feature_available_time_ms`, `htf_1h_close_time_ms`, and `htf_4h_close_time_ms`. The Direction-Aligned runner requires these columns for Funding-aware research, checks that the feature timestamp equals the closed 15m signal timestamp, verifies 1h/4h UTC close boundaries, rejects future timestamps, and rejects stale higher-timeframe context.

## Extreme-event audit

Each label run writes `extreme_event_audit.json` containing the raw Trade and Mark Price 15m OHLC window around feature outliers. This is evidence for manual source verification; the pipeline does not clip, winsorize, or silently remove extremes.

## Research runner

Run Direction-Aligned Logistic Regression diagnostics only against the Funding-aware output directory. The runner refuses Price-only or mixed label scopes. A successful run is not model approval; Funding retrieval provenance and the missing margin/liquidation model remain explicit blockers.
