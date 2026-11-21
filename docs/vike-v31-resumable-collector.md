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

Checkpoint schema version 3 records each stream's next cursor, committed row
count, last committed timestamp, committed CSV byte offset, and SHA-256 of
the committed CSV. CSV pages are flushed and fsynced before the checkpoint is
atomically replaced.

If the process stops after a CSV page is durable but before its checkpoint is
replaced, the next run truncates the uncommitted CSV tail back to the last
checkpointed byte offset and requests that page again. This prevents duplicate
rows from that crash window. If the CSV is shorter than the checkpoint, its
header or row widths are wrong, timestamps are not strictly increasing, or
its row count/last timestamp or SHA-256 disagrees with the checkpoint, the
collector fails closed rather than guessing.

Trade and Mark Price kline rows must each have exactly 12 fields, but they
use separate CSV schemas. Trade rows retain Binance trade-volume and trade
count column names. Mark Price rows use mark_open, mark_high, mark_low, and
mark_close; tuple positions 5 and 7-11 are preserved as ignored_5 and
ignored_7 through ignored_11, not mislabeled as trade volume or trade
statistics. Funding rows must contain fundingTime, fundingRate, and markPrice.

The schema change requires checkpoint version 3. Existing version 2
checkpoints are intentionally rejected rather than silently reinterpreted.
Use a new output directory when collecting with this version; keep prior CSVs
for audit and comparison. A header-only CSV left by a crash
during initial setup can be safely adopted. A CSV containing data but no
matching checkpoint is rejected. Checkpoints from schema version 1 are not
silently migrated; use a new output directory and independently audit any
older data before reuse.

## Important limitation

An empty API page for Trade or Mark is recorded as an unresolved tail; the
stream remains incomplete and can be retried by running the same command.
Kline requests explicitly send interval=15m.

For Funding, an empty page terminates pagination for the requested bounded
query and records termination_reason=api_empty_page. This sets the download
pagination state to complete, but coverage_verified remains false. It does
not prove that every expected historical funding event was returned. A
separate funding-coverage audit must compare the downloaded event timestamps
against the authoritative source and expected contract history before any
training use. Never synthesize missing funding events or rates.

Resume/crash-recovery tests cover Trade, Mark, and Funding, including the
CSV-fsync-before-checkpoint failure window. The CSV must still pass a separate
independent audit for chronology,
duplicates, gaps, expected interval, funding event coverage, and cross-feed
timestamp alignment. A completed download is not a training-ready dataset.

The next required stages remain: historical contract/universe metadata,
causal 15m/1h/4h feature and Donchian event preparation, Triple-Barrier labels,
fee/slippage accounting, historical funding cashflows, synchronized mark-price
liquidation checks, overlap control, immutable dataset manifest and hash, and
the Vike V3.1 chronological validation/acceptance protocol. Do not invoke the
legacy external-wallet Advisor approval path for Binance market events.
