# Vike V3.1 — Direction-Aligned Research

## Purpose and scope

This is a reproducible research runner for the already-generated Price-only label bundle. It operationalizes the Direction-Aligned Logistic Regression experiment and emits fold-level plus symbol × direction × timeframe × period diagnostics.

It is **not** a model-approval pipeline. The source labels exclude events crossing Funding settlements and do not model Funding cashflows, leverage, margin, maintenance brackets, or liquidation. The verified-label research path remains the approval candidate. LightGBM is deliberately not included.

The runner is read-only with respect to its source bundle. It does not access raw-staging, make network calls, download data, upload to Drive, or read the locked May–October 2026 holdout.

## Frozen input

Use the existing bundle:

`G:\Mon Drive\ai trade\Vike V3.1\derived\price-only-labels\20261124T115641Z`

It must contain:
- `labels.csv`
- `research_report.json`
- `dataset_manifest.json`

The source report must identify the 15m Price-only label scope and explicitly state that the locked holdout was not accessed and model training was not started. The script checks these flags and verifies every included event remains inside November 2023–April 2026.

Only rows with `label_status=LABELED_PRICE_ONLY` are modeled. Excluded candidates remain represented in the original source bundle and are not silently relabeled.

## Direction-Aligned feature map

The 14-feature vector is direction-neutral: positive directional values always mean movement aligned with the event's LONG/SHORT side.

| Source feature | Model feature / transform |
|---|---|
| ret_1, ret_4, ret_16 | Multiply by +1 for LONG, −1 for SHORT |
| htf_1h_ret_1/4 | Multiply by direction sign |
| htf_4h_ret_1/4 | Multiply by direction sign |
| channel_position | Distance outward from the relevant Donchian edge: LONG = position − 1; SHORT = −position |
| rsi14 | LONG = RSI; SHORT = 1 − RSI |
| atr_pct | Natural logarithm |
| donchian_width_pct | Natural logarithm |
| volume_ratio | log(1 + volume ratio) |
| hour_sin, hour_cos | Unchanged |

The runner requires normalized RSI in [0, 1], positive ATR/channel width, finite numeric inputs, and a closed 15m signal timestamp. No label, net R, exit price, funding, or execution-cost field is used as a model feature.

## Model and evaluation protocol

- One pooled meta-label model across the frozen eight-symbol universe and both directions; direction is represented by the aligned features.
- StandardScaler + regularized Logistic Regression.
- No class balancing.
- Default C grid: 0.001, 0.01, 0.1, 1, 10.
- C is selected by ROC-AUC on an inner chronological 80/20 split of the pre-test training history. The scaler is inside the sklearn Pipeline and is fitted only on the relevant training partition.
- The take/abstain threshold is the 66.7th percentile of probabilities from the inner-validation model. It is fixed before OOS scoring; test labels never select C or the threshold.
- The final fold model is refitted on the complete eligible pre-test training window after C and threshold selection.
- 48-bar label maturity purge/embargo at temporal boundaries.
- Three expanding four-month OOS periods:

| Fold | Training history ends before | OOS period |
|---|---|---|
| 1 | 2025-05-01 UTC | May–August 2025 |
| 2 | 2025-09-01 UTC | September–December 2025 |
| 3 | 2026-01-01 UTC | January–April 2026 |

The final OOS period ends at 2026-05-01 UTC. The May–October 2026 locked holdout is not opened.

## Fail-closed gates

A fold is inconclusive unless it has:
- at least 3,000 pooled eligible training labels;
- at least 200 eligible training labels for every observed symbol × direction × timeframe group;
- at least 50 matured OOS labels;
- enough two-class inner-training and validation observations to select C.

The aggregate diagnostics also record whether there are at least three scored folds, at least 100 OOS events, and a positive lower bound for the 95% uplift interval. The interval uses a deterministic UTC-week cluster bootstrap so events from the same week across symbols remain together.

These diagnostic gates do not override the Price-only label limitation. The report always says `NOT_APPROVED_PRICE_ONLY_SCOPE`.

## Windows PowerShell

From the repository root:

```powershell
python -m pip install -r requirements-direction-aligned.txt

$inputDir = "G:\Mon Drive\ai trade\Vike V3.1\derived\price-only-labels\20261124T115641Z"
$outputDir = "G:\Mon Drive\ai trade\Vike V3.1\derived\direction-aligned-research\run-20261124"

python .\scripts\vike_direction_aligned_research.py `
  --input-dir $inputDir `
  --output-dir $outputDir
```

The output directory must not be inside the input bundle or raw-staging. Choose a new, unused output directory for every run.

## Outputs

- `direction_aligned_report.json`: frozen configuration, fold outcomes, group-level results, uncertainty interval, and diagnostic gates.
- `direction_aligned_report.md`: concise fold summary and interpretation.
- `oos_predictions.csv`: OOS-only predictions, selected/abstained decisions, labels, net R, and aligned features.
- `run_manifest.json`: input hashes, script hash, runtime versions, fold settings, and safety flags.

The group table contains one row per symbol × direction × timeframe × fold, including explicit INCONCLUSIVE rows when a fold cannot be scored.

## Interpretation

The earlier exploratory result (AUC 0.513; Top-Tercile mean −0.347R versus All-Signals mean −0.385R; uplift +0.038R; 95% interval −0.057R to +0.128R) is weak and statistically inconclusive. This script is intended to make the experiment reproducible and expose group/fold instability, not to validate that result in advance.

Do not train LightGBM, export a model for execution, approve a strategy, or connect this research artifact to paper/live execution based on this report alone.
