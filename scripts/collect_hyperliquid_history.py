"""Resumable four-year Hyperliquid OHLCV collection and coverage audit.

Downloads bounded Vike export chunks and checkpoints each chunk independently.
No strategy backtest is run by this collector.
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://data.vike.io/v1/hyperliquid"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
# Explicit research horizons agreed for the data-coverage phase.
HORIZONS = {"15m": 730, "1h": 1095, "4h": 1460, "1d": 1460}
MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def utc(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("UTC timestamps must include timezone")
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_chunk(symbol, timeframe, start, end, key, retries=5):
    query = urlencode({
        "symbol": symbol, "interval": timeframe, "market": "perp",
        "start": iso(start), "end": iso(end), "limit": 5000,
    })
    req = Request(f"{API}/ohlcv/export?{query}", headers={
        "X-API-KEY": key, "Accept": "application/x-ndjson",
    })
    last_error = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=120) as response:
                raw = response.read().decode("utf-8")
            rows = []
            for n, line in enumerate(raw.splitlines(), 1):
                if not line.strip():
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict) or "ts" not in obj:
                    continue
                rows.append([
                    int(obj["ts"]), float(obj["open"]), float(obj["high"]),
                    float(obj["low"]), float(obj["close"]), float(obj["volume"]),
                ])
            rows.sort(key=lambda x: x[0])
            if len({x[0] for x in rows}) != len(rows):
                raise ValueError(f"Duplicate timestamps in {symbol} {timeframe} chunk")
            return rows
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Failed fetching {symbol} {timeframe} {iso(start)}: {last_error}")


def coverage(rows, timeframe, requested_start, requested_end):
    step = MINUTES[timeframe] * 60_000
    expected_start = int(requested_start.timestamp() * 1000)
    expected_end = int(requested_end.timestamp() * 1000)
    timestamps = [int(r[0]) for r in rows if expected_start <= int(r[0]) < expected_end]
    unique = sorted(set(timestamps))
    expected = max(0, (expected_end - expected_start) // step)
    gaps = []
    if unique:
        previous = unique[0]
        for current in unique[1:]:
            missing = (current - previous) // step - 1
            if missing > 0:
                gaps.append({"after_ms": previous, "before_ms": current, "missing_bars": missing})
            previous = current
    missing_total = max(0, expected - len(unique))
    return {
        "requested_start_utc": iso(requested_start),
        "requested_end_utc": iso(requested_end),
        "first_candle_utc": datetime.fromtimestamp(unique[0] / 1000, timezone.utc).isoformat() if unique else None,
        "last_candle_utc": datetime.fromtimestamp(unique[-1] / 1000, timezone.utc).isoformat() if unique else None,
        "expected_bars": expected,
        "actual_unique_bars": len(unique),
        "missing_bars_estimate": missing_total,
        "coverage_pct": round(100 * len(unique) / expected, 4) if expected else 0.0,
        "gap_segments": len(gaps),
        "largest_gap_bars": max((g["missing_bars"] for g in gaps), default=0),
        "gaps": gaps,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--end", default=iso(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)))
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--timeframes", default="15m,1h,4h,1d")
    p.add_argument("--output-dir", default="data/strategy-research/hyperliquid-4y")
    p.add_argument("--chunk-days", type=int, default=28)
    args = p.parse_args()
    key = os.environ.get("VIKE_API_KEY", "").strip()
    if not key:
        p.error("VIKE_API_KEY is required; never paste or commit it")
    end = utc(args.end)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    timeframes = [x.strip() for x in args.timeframes.split(",") if x.strip()]
    if not symbols or not timeframes or args.chunk_days < 1:
        p.error("symbols/timeframes must be non-empty and chunk-days positive")
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for symbol in symbols:
        for tf in timeframes:
            if tf not in HORIZONS:
                raise ValueError(f"Unsupported timeframe {tf}; choose {sorted(HORIZONS)}")
            start = end - timedelta(days=HORIZONS[tf])
            interval = timedelta(minutes=MINUTES[tf])
            # One indicator warm-up year is not needed: 200 bars precede the scoring horizon.
            fetch_start = start - interval * 200
            cache_dir = root / symbol / tf / "chunks"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cursor = fetch_start
            chunk_index = 0
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=args.chunk_days), end)
                chunk_file = cache_dir / f"{cursor.strftime('%Y%m%dT%H%M%S')}_{chunk_end.strftime('%Y%m%dT%H%M%S')}.json"
                if chunk_file.exists():
                    chunk_index += 1
                    cursor = chunk_end
                    continue
                print(f"FETCH {symbol} {tf} {iso(cursor)} -> {iso(chunk_end)}", flush=True)
                rows = fetch_chunk(symbol, tf, cursor, chunk_end, key)
                tmp = chunk_file.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
                tmp.replace(chunk_file)
                print(f"CHECKPOINT {symbol} {tf} chunk={chunk_index} candles={len(rows)}", flush=True)
                chunk_index += 1
                cursor = chunk_end
            all_rows = []
            for file in sorted(cache_dir.glob("*.json")):
                all_rows.extend(json.loads(file.read_text(encoding="utf-8")))
            by_ts = {int(row[0]): row for row in all_rows}
            merged = [by_ts[k] for k in sorted(by_ts)]
            report = coverage(merged, tf, start, end)
            result = {"exchange": "hyperliquid", "market": "perp", "symbol": symbol,
                      "timeframe": tf, "history_days": HORIZONS[tf],
                      "source": "Vike Hyperliquid OHLCV export", **report}
            results.append(result)
            data_file = root / f"{symbol}_{tf}.json"
            data_file.write_text(json.dumps({
                "exchange": "hyperliquid", "symbol": symbol, "market_type": "futures",
                "timeframe": tf, "candles": merged, "source": result["source"],
                "coverage": report,
            }, separators=(",", ":")), encoding="utf-8")
            print(f"COVERAGE {symbol} {tf}: {report['coverage_pct']}% "
                  f"({report['actual_unique_bars']}/{report['expected_bars']}) "
                  f"missing~{report['missing_bars_estimate']}", flush=True)
            # Persist the report after each completed group so interruption loses no audit.
            (root / "coverage-report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Coverage report: {root / 'coverage-report.json'}", flush=True)


if __name__ == "__main__":
    main()
