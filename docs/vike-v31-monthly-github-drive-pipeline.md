# Vike V3.1 monthly GitHub Actions + Google Drive pipeline

## Status: local Windows collection-integrity stage implemented / not training-enabled

This branch defines the safe execution contract for the monthly pipeline. It does
not claim that monthly labels, model fitting, or Google Drive transfer are
implemented. The existing collector and labeler are building blocks only.

## Frozen collection window

- First month: 2023-11
- Last month: 2026-10
- 36 UTC calendar months inclusive
- Development: 2023-11 through 2026-04 (30 months)
- Locked holdout: 2026-05 through 2026-10 (6 months)
- Symbols: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT
- Streams: trade_15m, mark_15m, funding

## Monthly state machine

For each (month, symbol, stream), persist a state record with:
requested UTC half-open window, source URL/archive identity, byte count, SHA-256,
row count, first/last timestamp, audit status, exclusion reasons, and code version.

States:
1. PLANNED
2. DOWNLOADED
3. CHECKSUM_VERIFIED
4. STRUCTURAL_AUDIT_PASS
5. COVERAGE_VERIFIED
6. LABELS_CREATED
7. FEATURES_PERSISTED
8. TRAINING_UPDATED (development months only)
9. DRIVE_COMMIT_VERIFIED
10. RAW_TEMP_DELETED

A failed state must stop that partition. Never advance a month on an unverified
Funding or Mark Price feed. An empty Funding REST page is pagination exhaustion,
not proof of complete coverage. Funding archive reconciliation remains
NOT_VERIFIED until Binance archive field semantics and completeness are
authoritatively established.

## Missing-day policy

A UTC day is eligible only if all expected 15-minute Trade and Mark Price bars
are present, unique, correctly aligned, and valid. If any expected bar is absent
or malformed, exclude the entire UTC day for that symbol. Do not interpolate,
forward-fill, synthesize, or bridge across excluded days. Events whose feature
lookback or label holding horizon intersects an excluded interval are excluded.
Persist the excluded-day ledger.

## Labels and accounting

Use the existing research-only Triple Barrier implementation only after all
preconditions are met:
- Wilder ATR(14), computed causally from eligible Trade bars.
- Stop 1.5 ATR, target 3 ATR, maximum 48 complete 15-minute bars.
- Entry at the next bar open.
- Actual verified Mark Price bars and independently verified liquidation inputs.
- Actual Funding events with verified coverage.
- Same-bar stop/target: stop first.
- Fees and adverse slippage must be explicit, versioned inputs.
- No incomplete horizon, missing feed, ambiguous funding settlement, or
  unverified liquidation result may become a label.

## Training isolation

- Only the first 30 months may be used for feature/model development and
  walk-forward fitting.
- The final six months must not be downloaded into the training workspace,
  featurized for tuning, used for threshold selection, or used for early stopping.
  Keep them in a separate locked Drive location with restricted workflow access.
- Monthly incremental fitting must be reproducible. Store immutable per-month
  feature/label partitions and model snapshots; do not merely overwrite a model.
- Final holdout is evaluated once after development is frozen.
- Acceptance is per independent group and requires the frozen V3.1 gates;
  sample count alone is not acceptance.

## Google Drive storage contract

Suggested Drive layout:
- vike-v31/raw-staging/YYYY-MM/SYMBOL/STREAM/ (temporary)
- vike-v31/derived/YYYY-MM/SYMBOL/ (features, labels, exclusions)
- vike-v31/manifests/YYYY-MM/ (checksums, audit JSON, provenance)
- vike-v31/models/development/YYYY-MM/ (model snapshots and metrics)
- vike-v31/locked-holdout/ (separate access; no training workflow permissions)

Never store Drive OAuth tokens or service-account JSON in the repository.
Use GitHub Actions encrypted secrets / environment secrets. Prefer a dedicated
service account with access only to the Vike Drive folder. Google Drive connector
availability in ChatGPT is not required for Actions, but the Actions identity
must be provisioned by the repository owner.

## Implemented Google Drive desktop monthly runner

Run from the repository Worktree on Windows PowerShell:

```powershell
.\scripts\run_vike_month_windows.ps1 -Month 2023-11 -DriveRoot "G:\Mon Drive\ai trade\Vike V3.1" -LocalRoot "C:\Vike_Data\Vike_V3.1" -Symbols BTCUSDT
```

The runner:
1. writes collector data and resumable per-symbol checkpoints directly into the Google Drive for desktop sync folder (`raw-staging/YYYY-MM`);
2. requires complete Trade, Mark, and Funding stream checkpoints for every requested symbol;
3. audits each UTC day independently: exactly 96 valid 15-minute Trade and Mark bars per feed are required; a deficient day is marked EXCLUDE_DAY and is never filled;
4. requires Funding CSV structural validity, but always leaves historical Funding coverage NOT_VERIFIED;
5. writes month-audit.json, manifest.json, and manifest.json.sha256;
6. copies the month to the local Google Drive for desktop sync path and verifies every manifest-listed file and the manifest sidecar by size and SHA-256.

The local hash check proves the destination folder copy, not that the cloud upload
has completed. Confirm Google Drive for desktop reports Synced. Funding coverage
is still unverified, all label_eligible flags remain false, and this output is
not model approval. The runner retains raw staging in the Google Drive sync folder and never deletes it.

The first pilot should be one symbol and one development month only. Do not scale
to all eight symbols or all 30 development months until the pilot report, hashes,
Drive sync state, and resume behavior have been reviewed. The locked holdout
months 2026-05 through 2026-10 are rejected by both runner and audit utility.

## Deletion / recovery rules

Raw staging may be deleted only after:
1. upload completes;
2. remote object size and SHA-256 are independently re-read and match;
3. derived data, audit reports, manifest, and checkpoint are committed to Drive;
4. a durable completion marker is written.

Keep derived features/labels, manifests, exclusion ledgers, model snapshots,
metrics, and checkpoints. A failed upload or failed hash check must retain raw
staging and block the next month. Do not use GitHub Actions cache as the dataset
store; cache is not durable provenance storage.

## Required implementation before enabling training

1. A Drive client with least-privilege authentication and upload/download hash
   verification.
2. A monthly runner that calls the collector with exact month boundaries and
   resumes from durable per-partition checkpoints.
3. Independent per-symbol/month Trade and Mark Price audits.
4. Funding archive reconciliation plus authoritative semantic/coverage proof;
   otherwise the month is REJECTED for labels.
5. Historical contract listing/availability metadata and point-in-time universe.
6. Causal feature builder, Wilder ATR(14), overlap/purge controls, and a complete
   label dataset builder using the existing Triple Barrier function.
7. Reproducible Logistic Regression baseline and LightGBM challenger, walk-forward
   evaluation, synchronized block bootstrap, and locked holdout runner.
8. Tests for interrupted uploads, checksum mismatch, reruns, missing days,
   funding coverage false, holdout access denial, and deletion guard.
9. A manual dispatch workflow first; schedule only after a complete end-to-end
   dry run passes.

## Current known implementation boundary

The repository already has a resumable Binance USD-M collector, structural
Funding and Mark Price audit utilities, Funding archive reconciliation, and a
research-only Triple Barrier label function. The collector deliberately leaves
Funding coverage unverified. The label function requires verified Funding,
verified Mark Price coverage, and a PASS liquidation verification object.
There is not yet a complete monthly orchestration, Drive transfer, causal ATR /
feature dataset pipeline, or V3.1 model acceptance runner. Therefore this branch
must remain draft and must not start a training run or delete source data.
