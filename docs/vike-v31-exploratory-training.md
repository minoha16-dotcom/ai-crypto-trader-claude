# Vike V3.1 — Drive-backed exploratory training

## Purpose and safety boundary

This local, read-only stage consumes the existing Google Drive desktop dataset:
the 30 development months (2023-11 through 2026-04) and the eight frozen
Binance USD-M symbols. It never calls an exchange, uploads data to GitHub, or
changes, moves, or deletes anything in raw-staging.

Outputs go only to:
G:\Mon Drive\ai trade\Vike V3.1\derived\exploratory-training\<run-id>\

Each UTC day is eligible only when Trade and Mark Price both contain exactly
96 aligned 15m candles. Missing or incomplete days are excluded and windows
never bridge a gap. Missing or ambiguous monthly files fail closed.

## Provisional labels are not final V3.1 labels

Funding coverage remains NOT_VERIFIED and historical liquidation inputs are
not proven. This pass therefore does not call the Triple Barrier labeler and
does not use Funding as a feature or label input.

The provisional target is a fixed-horizon proxy:
- Direction comes from the frozen direction-neutral Donchian-20 breakout.
- Entry is the next 15m open.
- Exit is the close of the 48th holding candle.
- Round-trip fee and adverse-slippage assumptions are explicit inputs.
- Positive class means net forward return after those costs is positive.
- net_r_proxy divides net return by the 1.5 ATR initial stop distance.

This checks the Drive loader, event generation, feature pipeline and baseline
fitting. It is not the approved Triple-Barrier meta-label and cannot satisfy
model acceptance.

## Features and leakage controls

Features use only information available at the signal candle close. The 1h
and 4h context is aggregated from complete 15m bars; only context candles
whose close timestamp is at or before the signal close are used. Logistic
Regression scaling is fitted on training data only.

Events are sorted chronologically. The last 30% is a diagnostic test segment.
Training events whose label exit reaches or crosses the test start are purged.
This single split is exploratory only, not the required three-fold walk-forward.
The locked May-Oct 2026 holdout is never opened.

## Install and run on Windows PowerShell

From the repository root:

    python -m pip install -r requirements-vike-research.txt

    python .\scripts\vike_exploratory_train.py --drive-root "G:\Mon Drive\ai trade\Vike V3.1" --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --fee-bps-per-side 5 --slippage-bps-per-side 2

Input layout:
- raw-staging/YYYY-MM/SYMBOL/trade_15m.csv
- raw-staging/YYYY-MM/SYMBOL/mark_15m.csv
- raw-staging/YYYY-MM/SYMBOL/funding.csv

The flat raw-staging/YYYY-MM/SYMBOL-stream.csv layout is also supported.
Funding is structurally audited and hashed, but remains NOT_VERIFIED.

## Output artifacts

Each run creates a unique directory under derived/exploratory-training:
- dataset_manifest.json: source hashes, monthly audit counts and run settings.
- events.csv: provisional event outcomes and features.
- provisional_logistic_regression.joblib: research-only fitted model.
- exploratory_report.json: counts, diagnostic metrics and explicit blockers.
- excluded_days.json: per-symbol/month excluded UTC days.

Do not copy this model into production or paper execution. Do not use these
labels for model acceptance. The next stage must resolve Funding provenance
and coverage, historical liquidation evidence, the final Triple-Barrier
dataset builder, overlap control, three or more valid walk-forward folds,
and the frozen V3.1 acceptance protocol.
