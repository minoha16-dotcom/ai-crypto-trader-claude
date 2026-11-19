# Vike V3.1 — Stage 1: historical data coverage

The first implementation stage adds:
- `backend/historical_data_validation.py`: strict Unix epoch ms/µs normalization and timestamp-series diagnostics. Seconds, ambiguous magnitudes, implausible dates, duplicates and non-chronological observations are not silently accepted. Gaps are reported, never filled.
- `scripts/binance_usdm_coverage_probe.py`: read-only public REST probe for Binance USD-M USDT perpetuals. It checks current contract metadata and first/recent boundary observations for trade 15m klines, funding history and mark-price 15m klines.

## Run locally

From the repository root (Python environment with `requests` installed):

```powershell
python -m pytest tests/test_historical_data_validation.py -q
python scripts/binance_usdm_coverage_probe.py
```

Optional explicit window and symbols:

```powershell
python scripts/binance_usdm_coverage_probe.py --start 2023-11-19T00:00:00Z --end 2026-11-19T00:00:00Z --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,ADAUSDT --out artifacts/binance-usdm-coverage.json
```

The default window is three calendar years ending at run time. The script writes a JSON report and exits nonzero if any currently trading requested symbol lacks one of the three dataset boundaries.

## Interpretation — important

A `PASS_BOUNDARIES_ONLY` result means only that the public API returned observations near both requested boundaries for all three datasets. It does **not** prove:
- complete 3-year continuity or absence of internal gaps;
- full historical mark-price/funding availability;
- historical listing/delisting completeness or survivorship-free universe selection;
- executable spread, order-book depth or realistic fills.

The next stage must download paginated history, validate every timestamp and checksum where supplied, quantify missing intervals, compare coverage across all symbols, and produce a frozen dataset manifest. Do not train a model from this probe report.

The probe uses public endpoints only, does not require keys, submits no orders, and does not use a paid service. If Binance responds with rate limiting or an IP restriction, record that as an operational limitation; do not repeatedly hammer the API.
