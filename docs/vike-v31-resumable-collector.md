# Vike V3.1 resumable data collector

## Purpose and safety

This collector retrieves public Binance USD-M Futures 15-minute trade klines,
15-minute mark-price klines, and funding-rate events. It is research-only:
there are no credentials, private endpoints, orders, labels, training, or model
approval. It never fills missing bars or funding events.

Raw CSV files and checkpoint JSON are local research data. Do not commit them,
upload them as workflow artifacts, or place them in GitHub Actions cache.

## Example

~~~powershell
python scripts/binance_usdm_resumable_collector.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --start 2023-11-01T00:00:00Z --end 2023-12-01T00:00:00Z --out-dir C:\Vike_Data\binance-v31-nov2023
~~~

Run the exact same command again to resume. The checkpoint scope (symbol and
[start, end) window) must match. Each stream advances independently and the
checkpoint is atomically replaced after each durable page. Funding pagination
advances from the actual returned event timestamp by 1 ms; it does not assume
exact 8-hour multiples.

## Crash recovery and file integrity

Checkpoint schema version 2 records each stream's next cursor, committed row
count, last committed timestamp, and committed CSV byte offset. CSV pages are
flushed and fsynced before the checkpoint is atomically replaced.

If the process stops after a CSV page is durable but before its checkpoint is
replaced, the next run truncates the uncommitted CSV tail back to the last
checkpointed byte offset and requests that page again. This prevents duplicate
rows from that crash window. If the CSV is shorter than the checkpoint, its
header or row widths are wrong, timestamps are not strictly increasing, or
its row count/last timestamp disagrees with the checkpoint, the collector
fails closed rather than guessing.

Kline rows must have exactly 12 fields and funding rows must contain
fundingTime, fundingRate, and markPrice. A header-only CSV left by a crash
during initial setup can be safely adopted. A CSV containing data but no
matching checkpoint is rejected. Checkpoints from schema version 1 are not
silently migrated; use a new output directory and independently audit any
older data before reuse.

## Important limitation

An empty API page is recorded as an unresolved tail and is not treated as
proof of continuous coverage. The stream remains incomplete and can be
retried by running the same command again. In particular, do not infer that
bars or funding events were continuous merely because the request returned
an empty page.

The CSV must still pass a separate independent audit for chronology,
duplicates, gaps, expected interval, funding event coverage, and cross-feed
timestamp alignment. A completed download is not a training-ready dataset.

The next required stages remain: historical contract/universe metadata,
causal 15m/1h/4h feature and Donchian event preparation, Triple-Barrier labels,
fee/slippage accounting, historical funding cashflows, synchronized mark-price
liquidation checks, overlap control, immutable dataset manifest and hash, and
the Vike V3.1 chronological validation/acceptance protocol. Do not invoke the
legacy external-wallet Advisor approval path for Binance market events.
