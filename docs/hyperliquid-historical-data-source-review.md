# Hyperliquid historical source coverage decision

Checked 18 November 2026.

## Vike OHLCV

Vike's published Hyperliquid time-bar history starts at 2025-12-31 23:59 UTC (first candle open: 2026-01-01 00:00 UTC). It cannot supply the agreed 2–4 year windows. The previous collector treated empty pre-2026 responses as successful cached chunks and its coverage report omitted leading/trailing gaps. Both defects are fixed in the collector.

## Candidate alternative: Tardis.dev

Tardis documents Hyperliquid historical market data from 2024-10-29. This can cover the requested 15m two-year window starting 2024-11-18, subject to symbol/channel coverage checks. It cannot cover the requested 1h three-year start in November 2023, nor the 4h/1d four-year starts in November 2022. Tardis offers historical API access by subscription; monthly first-day CSV samples are not a complete continuous dataset.

## Other sources

Hyperliquid's own Info API only exposes the most recent 5,000 candles, so it is not a universal four-year backfill. Hyperliquid's S3 archive documents L2 snapshots and asset contexts, plus node fills in separate formats; it does not provide a ready-made historical OHLCV archive. A third-party archive such as 0xArchive advertises deeper historical trades, but full retained access is a paid plan and needs independent symbol/date coverage verification before use.

## Decision

Do not launch the multi-year bulk collection or any strategy training from Vike. Run the new Vike preflight only to confirm the live endpoint's old-vs-recent response. For a complete backtest dataset, select and authorize a source with the required history, then validate each symbol's first/last timestamps and gaps before importing. HYPE cannot have four years of native Hyperliquid market history because the market/token did not exist for that full period.
