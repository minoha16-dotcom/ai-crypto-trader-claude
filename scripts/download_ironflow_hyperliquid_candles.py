"""Download Ironflow's public Hyperliquid hourly-candle Parquet archive.

No AWS account, API key, or paid subscription is required. The public dataset
is third-party data (not an official Hyperliquid publication). Downloads are
resumable at monthly-file granularity. 4h and 1d bars are derived only from
complete UTC buckets of 1h candles; 15m is intentionally unsupported.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATASET = "ironflow/hyperliquid-candles-1h"
BASE_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main/data"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
START_MONTH = "2025-05"
END_MONTH = "2026-10"
TIMEFRAMES = ("1h", "4h", "1d")
ALIASES = {
    "timestamp": ("timestamp", "time", "ts", "t"),
    "market": ("market", "symbol", "coin", "s"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "vol", "v"),
}


def month_range(start: str, end: str):
    def parse(value):
        try:
            year_text, month_text = value.split("-")
            year, month = int(year_text), int(month_text)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"month must use YYYY-MM format: {value!r}") from exc
        if len(year_text) != 4 or len(month_text) != 2 or year < 2000 or not 1 <= month <= 12:
            raise ValueError(f"invalid month: {value!r}")
        return year, month

    sy, sm = parse(start)
    ey, em = parse(end)
    if (ey, em) < (sy, sm):
        raise ValueError("end month must not precede start month")
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year, month = year + 1, 1


def download_month(month: str, raw_dir: Path, retries: int = 3) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{month}.parquet"
    if target.exists() and target.stat().st_size > 0:
        return target
    url = f"{BASE_URL}/{month}.parquet"
    last_error = None
    for attempt in range(retries):
        temp = target.with_suffix(".parquet.part")
        try:
            request = Request(url, headers={"User-Agent": "ai-crypto-trader-research/1.0"})
            with urlopen(request, timeout=180) as response, temp.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            if temp.stat().st_size == 0:
                raise ValueError(f"Empty dataset file for {month}")
            temp.replace(target)
            return target
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            temp.unlink(missing_ok=True)
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise FileNotFoundError(f"Ironflow monthly Parquet file not found: {month}") from exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to download {month}: {last_error}") from last_error


def _column_map(columns):
    lowered = {str(c).lower(): c for c in columns}
    found = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                found[canonical] = lowered[alias]
                break
    missing = set(ALIASES) - set(found)
    if missing:
        raise ValueError(f"Unexpected Ironflow Parquet schema; missing {sorted(missing)}; columns={list(columns)}")
    return found


def _timestamp_ms(value):
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    number = int(value)
    # Hugging Face Arrow timestamp[us] may be decoded as integer microseconds.
    if number > 10**14:
        return number // 1000
    if number > 10**11:
        return number
    return number * 1000


def load_hourly(files, symbols):
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError("Install optional research dependencies: pip install -r requirements-research.txt") from exc

    wanted = {s.upper() for s in symbols}
    rows_by_symbol = defaultdict(list)
    schema_seen = None
    for path in files:
        frame = pl.read_parquet(path)
        columns = _column_map(frame.columns)
        schema_seen = list(frame.columns)
        selected = frame.select([columns[k] for k in ALIASES]).rename(
            {columns[k]: k for k in ALIASES}
        )
        for row in selected.iter_rows(named=True):
            market = str(row["market"]).upper()
            coin = market.replace("-PERP", "").replace("-SPOT", "")
            if coin not in wanted and market not in wanted:
                continue
            symbol = coin if coin in wanted else market
            ts = _timestamp_ms(row["timestamp"])
            values = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
            if not all(v == v and abs(v) != float("inf") for v in values):
                raise ValueError(f"Non-finite OHLCV row: {symbol} {ts}")
            o, h, l, c, volume = values
            if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h) or volume < 0:
                raise ValueError(f"Invalid OHLCV values: {symbol} {ts}")
            rows_by_symbol[symbol].append([ts, o, h, l, c, volume])
    if not schema_seen:
        raise ValueError("No Parquet files were read")
    return rows_by_symbol, schema_seen


def validate_hourly(symbol, rows):
    rows.sort(key=lambda r: r[0])
    unique = {}
    for row in rows:
        if row[0] % 3_600_000 != 0:
            raise ValueError(f"Non-UTC-hour-aligned candle: {symbol} {row[0]}")
        if row[0] in unique:
            if unique[row[0]] != row:
                raise ValueError(f"Conflicting duplicate candle: {symbol} {row[0]}")
            continue
        unique[row[0]] = row
    rows = list(unique.values())
    gaps = []
    for previous, current in zip(rows, rows[1:]):
        delta = current[0] - previous[0]
        if delta != 3_600_000:
            gaps.append({
                "after": datetime.fromtimestamp(previous[0] / 1000, timezone.utc).isoformat(),
                "before": datetime.fromtimestamp(current[0] / 1000, timezone.utc).isoformat(),
                "missing_hours": max(0, delta // 3_600_000 - 1),
                "ordering_or_alignment_error": delta <= 0 or delta % 3_600_000 != 0,
            })
    return rows, gaps


def aggregate(rows, hours):
    """Aggregate only complete UTC buckets; do not fabricate missing candles."""
    buckets = defaultdict(list)
    width = hours * 3_600_000
    for row in rows:
        bucket = (row[0] // width) * width
        buckets[bucket].append(row)
    result = []
    for bucket, members in sorted(buckets.items()):
        members.sort(key=lambda r: r[0])
        if len(members) != hours:
            continue
        if any(members[i][0] - members[i - 1][0] != 3_600_000 for i in range(1, hours)):
            continue
        if members[0][0] != bucket:
            continue
        result.append([
            bucket, members[0][1], max(r[2] for r in members),
            min(r[3] for r in members), members[-1][4],
            sum(r[5] for r in members),
        ])
    return result


def write_outputs(symbols, rows_by_symbol, output_dir: Path, source_files,
                  expected_start_ms: int, expected_end_ms: int):
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "source": "Ironflow public Hugging Face Parquet dataset",
        "dataset": DATASET,
        "dataset_url": f"https://huggingface.co/datasets/{DATASET}",
        "credit": "Ironflow (ironflow.sh)",
        "license": "CC BY 4.0 for files published before 2026-11-10; verify each file license before redistribution or commercial use",
        "requested_start": "2025-05-25T00:00:00Z",
        "requested_end_exclusive": "2026-11-01T00:00:00Z",
        "source_timeframe": "1h",
        "derived_timeframes": ["4h", "1d"],
        "unsupported_timeframes": ["15m"],
        "source_files": [p.name for p in source_files],
        "groups": {},
    }
    for symbol in symbols:
        if symbol not in rows_by_symbol:
            report["groups"][symbol] = {"status": "MISSING_SYMBOL", "rows_1h": 0, "rejection_reasons": ["NO_ROWS_FOR_SYMBOL"]}
            continue
        # Enforce the requested half-open UTC interval; monthly downloads can
        # include rows outside it, and a contiguous subset is not full coverage.
        requested_rows = [
            row for row in rows_by_symbol[symbol]
            if expected_start_ms <= row[0] < expected_end_ms
        ]
        hourly, gaps = validate_hourly(symbol, requested_rows)
        if not hourly:
            report["groups"][symbol] = {"status": "EMPTY", "rows_1h": 0, "rejection_reasons": ["NO_VALID_HOURLY_ROWS"]}
            continue
        for timeframe, hours in (("1h", 1), ("4h", 4), ("1d", 24)):
            bars = hourly if hours == 1 else aggregate(hourly, hours)
            path = output_dir / f"{symbol}_{timeframe}.json"
            payload = {
                "exchange": "hyperliquid",
                "symbol": symbol,
                "market_type": "futures",
                "timeframe": timeframe,
                "source_timeframe": "1h",
                "derived": hours != 1,
                "source": f"Ironflow:{DATASET}",
                "candles": bars,
            }
            path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        expected_last_ms = expected_end_ms - 3_600_000
        boundary_gaps = []
        if hourly[0][0] != expected_start_ms:
            boundary_gaps.append({
                "type": "MISSING_OR_LATE_START",
                "expected_utc": datetime.fromtimestamp(expected_start_ms / 1000, timezone.utc).isoformat(),
                "observed_utc": datetime.fromtimestamp(hourly[0][0] / 1000, timezone.utc).isoformat(),
            })
        if hourly[-1][0] != expected_last_ms:
            boundary_gaps.append({
                "type": "MISSING_OR_EARLY_END",
                "expected_utc": datetime.fromtimestamp(expected_last_ms / 1000, timezone.utc).isoformat(),
                "observed_utc": datetime.fromtimestamp(hourly[-1][0] / 1000, timezone.utc).isoformat(),
            })
        reasons = []
        if gaps:
            reasons.append("HOURLY_GAPS_PRESENT")
        if boundary_gaps:
            reasons.append("REQUESTED_WINDOW_NOT_FULLY_COVERED")
        report["groups"][symbol] = {
            "status": "STRUCTURALLY_CONTIGUOUS" if not reasons else "INCOMPLETE",
            "rows_1h": len(hourly),
            "expected_rows_1h": (expected_end_ms - expected_start_ms) // 3_600_000,
            "start_utc": datetime.fromtimestamp(hourly[0][0] / 1000, timezone.utc).isoformat(),
            "end_utc": datetime.fromtimestamp(hourly[-1][0] / 1000, timezone.utc).isoformat(),
            "boundary_gaps": boundary_gaps,
            "gaps": gaps,
            "rows_4h": len(aggregate(hourly, 4)),
            "rows_1d": len(aggregate(hourly, 24)),
            "rejection_reasons": reasons,
        }
    report["overall_status"] = (
        "READY_FOR_RESEARCH_VALIDATION"
        if all(report["groups"].get(s, {}).get("status") == "STRUCTURALLY_CONTIGUOUS" for s in symbols)
        else "REJECTED_OR_INCOMPLETE"
    )
    (output_dir / "coverage-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--start-month", default=START_MONTH)
    parser.add_argument("--end-month", default=END_MONTH)
    parser.add_argument("--raw-dir", default="data/ironflow/raw")
    parser.add_argument("--output-dir", default="data/ironflow/processed")
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        parser.error("--symbols must contain at least one symbol")
    files = [
        download_month(month, Path(args.raw_dir))
        for month in month_range(args.start_month, args.end_month)
    ]
    rows_by_symbol, schema = load_hourly(files, symbols)
    start_text = "2025-05-25T00:00:00+00:00"
    end_text = "2026-11-01T00:00:00+00:00"
    expected_start_ms = int(datetime.fromisoformat(start_text).timestamp() * 1000)
    expected_end_ms = int(datetime.fromisoformat(end_text).timestamp() * 1000)
    report = write_outputs(
        symbols, rows_by_symbol, Path(args.output_dir), files,
        expected_start_ms, expected_end_ms
    )
    print(json.dumps({"schema": schema, "overall_status": report["overall_status"], "groups": report["groups"]}, indent=2))
    return 0 if report["overall_status"] == "READY_FOR_RESEARCH_VALIDATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
