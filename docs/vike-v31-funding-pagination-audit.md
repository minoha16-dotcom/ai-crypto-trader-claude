# Funding pagination provenance audit — operator runbook

This procedure performs public, read-only requests to Binance USD-M
`GET /fapi/v1/fundingRate`, compares every returned funding timestamp, rate
and mark price with the existing frozen local CSV, and writes evidence only
under `derived/funding-audit/binance-pagination`.

It does **not** recollect or replace market data, write to `raw-staging`,
access the May–October 2026 holdout, use API credentials, or mark Funding
`VERIFIED` automatically.

## Run on the machine that has the frozen Drive archive

From PowerShell:

```powershell
Set-Location "C:\Vike_Data\worktrees\vike-v31-verified-label-walk-forward"
git pull --ff-only origin feat/vike-v31-verified-label-walk-forward

python -m py_compile .\scripts\vike_v31_funding_pagination_audit.py

python .\scripts\vike_v31_funding_pagination_audit.py `
  --drive-root "G:\Mon Drive\ai trade\Vike V3.1" `
  --pause-seconds 0.15
```

The script audits the eight frozen symbols over the 30 development months
(November 2023–April 2026). It paginates using the last returned
`fundingTime + 1 ms`, records each request's exact bounds and limit, saves
the exact response bytes, and records each response SHA-256.

## Expected outputs

- `derived/funding-audit/binance-pagination/funding-pagination-provenance.json`
- `derived/funding-audit/binance-pagination/api-responses/<SYMBOL>/<YYYY-MM>/page-NNN.json`

The summary reports expected/audited/passed/failed symbol-month groups and
whether any API or comparison errors occurred. A successful run should cover
240/240 groups and show zero timestamp or value differences. Inspect the
per-page request bounds, row counts, first/last timestamps and SHA-256 values;
the saved response bytes must remain alongside the report.

## Interpretation

A clean run is a strong retrieval-provenance packet and independent
local-vs-current-Binance comparison, but it is not by itself a human-reviewed
certificate. Keep `coverage_status=PENDING_INDEPENDENT_REVIEW` until a
reviewer verifies the endpoint semantics, page sequence, saved response
hashes and archive mapping. Never edit the report to claim `VERIFIED`
without that review.

If a request fails, preserve the partial output under `derived`, inspect the
specific error, and rerun only after determining whether the failure was
transient. Do not modify, delete, or regenerate any file in `raw-staging`.
