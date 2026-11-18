# Hyperliquid historical data source decision

Reviewed 18 November 2026.

## Adopted source: Tardis.dev

Tardis is the selected historical market-data provider for Hyperliquid. Its exchange-specific documentation states that Hyperliquid historical capture is available since 2024-10-29 and documents trades, l2Book, bbo, activeAssetCtx and related captured channels. The downloadable CSV trades schema provides exchange symbol, exchange timestamp in microseconds, local timestamp, price and amount.

The research OHLCV bars will be derived from the raw trades feed, not treated as a native Tardis candle feed. The aggregation must use exchange timestamps, UTC bucket boundaries, deterministic trade ordering and documented volume units. Missing intervals must remain gaps; do not forward-fill or fabricate candles.

## Coverage limits

- Tardis Hyperliquid collection begins 2024-10-29. It cannot provide native Hyperliquid data before that date.
- The agreed 15m two-year window begins 2024-11-18 (as of 2026-11-18) and is potentially coverable for symbols with matching instrument coverage.
- The agreed 1h three-year and 4h/1d four-year windows begin before Hyperliquid data collection started. These cannot be completed from Tardis Hyperliquid data.
- Each instrument has its own availableSince date. HYPE must use its actual Tardis instrument availability/listing metadata; do not assume the same history start as BTC/ETH/SOL.
- A four-year subscription entitlement does not create four years of exchange history. It only controls how much of the provider's existing history is accessible.
- Tardis downloadable CSV datasets for the first day of each month are available without an API key. Continuous daily history requires an active subscription/API key. Academic/Solo access is CSV-only; Pro/Business additionally include raw replay and instrument metadata APIs.

## Implemented integration

- scripts/preflight_tardis_hyperliquid.py checks Tardis exchange metadata, API-key entitlement and a first available daily trades CSV sample per requested symbol.
- .github/workflows/hyperliquid-tardis-preflight.yml provides a manual, read-only preflight workflow.
- The preflight does not download the full history, generate candles, train strategies, purchase a subscription or expose the API key.
- No bulk collector is launched until preflight confirms symbol availability, date access and CSV schema.

## Next gates

1. Add the secret TARDIS_API_KEY to GitHub repository Actions secrets (never commit or paste it into chat).
2. Run the Tardis preflight and inspect its report.
3. Implement/resume daily CSV trade ingestion and OHLCV aggregation only after the preflight passes.
4. Set each symbol's start date from actual provider metadata and report exact coverage, empty days, gaps, duplicates and off-grid bars.
5. Do not start strategy training until data coverage is accepted.

Vike is no longer the selected source for historical backfill. Keep the old Vike scripts only as legacy diagnostics until the Tardis path has passed validation.
