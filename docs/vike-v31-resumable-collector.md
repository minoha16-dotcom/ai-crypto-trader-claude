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

## Offline Funding structural audit

Run the independent CSV audit after collecting a bounded window:

~~~powershell
python scripts/audit_binance_usdm_funding.py --csv C:\\Vike_Data\\binance-v31\\BTCUSDT-funding.csv --start 2025-01-01T00:00:00Z --end 2025-02-01T00:00:00Z
~~~

The audit validates the exact Funding CSV schema, numeric rate and positive
mark price, duplicate and ordering checks, requested-window bounds, and
approximately 8-hour event cadence (60-second default tolerance). It emits
structural_status separately from coverage_status. Coverage always remains
NOT_VERIFIED: structural regularity, including first/last observed events,
cannot prove that the exchange returned every expected historical event.
A separate source/contract-history coverage verification is still required.


## Independent Funding archive reconciliation (new)

The Binance Vision USD-M monthly Funding archive is a second distribution path
for reconciliation. For a monthly partition, download both the ZIP and its
publisher checksum. Example for BTCUSDT January 2025:

~~~powershell
$base = "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT"
curl.exe -fL "$base/BTCUSDT-fundingRate-2025-01.zip" -o C:\\Vike_Data\\BTCUSDT-fundingRate-2025-01.zip
curl.exe -fL "$base/BTCUSDT-fundingRate-2025-01.zip.CHECKSUM" -o C:\\Vike_Data\\BTCUSDT-fundingRate-2025-01.zip.CHECKSUM
~~~

The archive CSV schema used by this reconciliation is
`calc_time,funding_interval_hours,last_funding_rate`. The REST collector schema
is `funding_time,funding_rate,mark_price`. The tool verifies the downloaded ZIP
against the publisher SHA-256, validates both schemas and numeric fields, then
performs one-to-one timestamp matching (60-second default tolerance) and rate
comparison (1e-10 default tolerance). It does not synthesize, interpolate, or
forward-fill records. REST mark_price is checked for finite positive values;
the archive does not supply a corresponding mark price in this schema.

Run:

~~~powershell
python scripts/reconcile_binance_usdm_funding_archive.py `
  --api-csv C:\\Vike_Data\\vike-v31-btcusdt-jan2025-v3\\BTCUSDT-funding.csv `
  --archive-zip C:\\Vike_Data\\BTCUSDT-fundingRate-2025-01.zip `
  --checksum C:\\Vike_Data\\BTCUSDT-fundingRate-2025-01.zip.CHECKSUM `
  --start 2025-01-01T00:00:00Z `
  --end 2025-02-01T00:00:00Z
~~~

Interpretation:
- `archive_checksum_verified=true` proves the local ZIP bytes match the
  publisher's checksum file.
- `archive_reconciliation_status=MATCH` means every API row and archive row
  matched one-to-one within the declared timestamp/rate tolerances.
- `MISMATCH` requires investigation; do not patch or fill the difference.
- `coverage_status` deliberately remains `NOT_VERIFIED` even on MATCH.
  Binance's public documentation currently describes the REST endpoint and
  its inclusive time bounds, ascending results and 1,000-row maximum, but the
  exact semantic mapping of archive `calc_time` to REST `fundingTime`, and
  archive `last_funding_rate` to REST `fundingRate`, plus archive
  completeness/finality and historical interval-change policy, still need
  authoritative confirmation. A checksum proves file integrity, not semantic
  equivalence or that an archive contains every expected event.

For a multi-month request, reconcile every month independently, including
missing archive months. A 404 or absent monthly file is a coverage gap, not a
zero-event month. Preserve each ZIP, checksum, and JSON report with the
dataset manifest.
