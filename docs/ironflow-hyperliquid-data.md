# Hyperliquid historical candles via Ironflow

## Scope

This research-only collector uses Ironflow's public Hugging Face dataset
(ironflow/hyperliquid-candles-1h) as a no-AWS alternative. It downloads
monthly Parquet files for BTC, ETH, SOL and HYPE, then produces 1h, 4h and 1d
OHLCV. The 4h and 1d bars are aggregated from complete UTC buckets of 1h data
only. Missing hours are never filled or interpolated.

15m is deliberately excluded: the public downloadable archive described by
Ironflow contains hourly candles. Its REST API's 15m historical window is
limited by account tier, so it is not treated as a free historical source.

## Run locally

```powershell
python -m pip install -r requirements.txt -r requirements-research.txt
python scripts/download_ironflow_hyperliquid_candles.py --symbols BTC,ETH,SOL,HYPE --start-month 2025-05 --end-month 2026-10
```

The collector is resumable by month: a non-empty monthly file already present
in data/ironflow/raw is reused. A failed transfer is written to a temporary
.part file and removed before retrying. Do not commit downloaded data.

Outputs in data/ironflow/processed:
- <SYMBOL>_1h.json, <SYMBOL>_4h.json, <SYMBOL>_1d.json
- coverage-report.json with observed start/end, row counts, missing hourly
  intervals, and an overall readiness status.

STRUCTURALLY_CONTIGUOUS means only that no interior hourly gap or conflicting
duplicate was found in the downloaded rows. It is not model approval and does
not prove that the symbol has the entire requested history. Review the report
before per-group walk-forward research. Groups with gaps or missing symbols
are rejected.

## Source and license

Dataset: https://huggingface.co/datasets/ironflow/hyperliquid-candles-1h
Provider: https://ironflow.sh/datasets
Credit: Ironflow (ironflow.sh).

Ironflow states that files published before 10 November 2026 are CC BY 4.0.
Verify the license of each exact file before redistribution or commercial use.
The source is third-party, not an official Hyperliquid dataset. This collector
does not access Requester Pays S3, AWS, or any paid API.

The resulting candles are for research only. They do not enable live trading
or constitute a strategy approval artifact.
