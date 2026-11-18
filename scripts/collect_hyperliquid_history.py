"""Resumable Hyperliquid OHLCV collector with strict source-coverage validation.

Vike's published history starts at 2025-12-31 23:59 UTC. The collector
refuses to label shorter history as a multi-year dataset. Use --preflight-only
to verify the live API before any bulk download.
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
VIKE_HISTORY_START = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
HORIZONS = {"15m": 730, "1h": 1095, "4h": 1460, "1d": 1460}
MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


class HistoryUnavailable(RuntimeError):
    """Raised when the requested window predates the source's documented history."""


def utc(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("UTC timestamps must include timezone")
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_export(raw, symbol, timeframe):
    rows = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        obj = json.loads(line)
        # The first NDJSON line is an envelope with symbol/market/source, not a candle.
        if not isinstance(obj, dict) or "ts" not in obj:
            continue
        try:
            row = [int(obj["ts"]), float(obj["open"]), float(obj["high"]),
                   float(obj["low"]), float(obj["close"]), float(obj["volume"])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed {symbol} {timeframe} candle at NDJSON line {line_no}") from exc
        if not all(map(lambda x: x == x and abs(x) != float("inf"), row[1:])):
            raise ValueError(f"Non-finite OHLCV value in {symbol} {timeframe}")
        rows.append(row)
    rows.sort(key=lambda x: x[0])
    timestamps = [r[0] for r in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError(f"Duplicate timestamps in {symbol} {timeframe} response")
    return rows


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
            return parse_export(raw, symbol, timeframe)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Failed fetching {symbol} {timeframe} {iso(start)}: {last_error}")


def coverage(rows, timeframe, requested_start, requested_end):
    step = MINUTES[timeframe] * 60_000
    start_ms = int(requested_start.timestamp() * 1000)
    end_ms = int(requested_end.timestamp() * 1000)
    expected = max(0, (end_ms - start_ms) // step)
    timestamps = sorted({int(r[0]) for r in rows if start_ms <= int(r[0]) < end_ms})
    off_grid = [ts for ts in timestamps if (ts - start_ms) % step != 0]
    aligned = [ts for ts in timestamps if (ts - start_ms) % step == 0]
    gaps = []
    cursor = start_ms
    for ts in aligned:
        if ts > cursor:
            missing = (ts - cursor) // step
            if missing > 0:
                gaps.append({"after_ms": cursor - step, "before_ms": ts, "missing_bars": missing})
        cursor = ts + step
    if cursor < end_ms:
        missing = (end_ms - cursor) // step
        if missing > 0:
            gaps.append({"after_ms": cursor - step, "before_ms": end_ms, "missing_bars": missing})
    actual = len(aligned)
    return {
        "requested_start_utc": iso(requested_start),
        "requested_end_utc": iso(requested_end),
        "first_candle_utc": datetime.fromtimestamp(timestamps[0] / 1000, timezone.utc).isoformat() if timestamps else None,
        "last_candle_utc": datetime.fromtimestamp(timestamps[-1] / 1000, timezone.utc).isoformat() if timestamps else None,
        "expected_bars": expected,
        "actual_unique_bars": len(timestamps),
        "aligned_bars": actual,
        "off_grid_bars": len(off_grid),
        "missing_bars_estimate": max(0, expected - actual),
        "coverage_pct": round(100 * actual / expected, 4) if expected else 0.0,
        "gap_segments": len(gaps),
        "largest_gap_bars": max((g["missing_bars"] for g in gaps), default=0),
        "gaps": gaps,
    }


def preflight(symbol, timeframes, key):
    """Compare one known-unavailable historical slice with a recent live slice."""
    old_end = VIKE_HISTORY_START
    report = {"source": "Vike Hyperliquid OHLCV export",
              "documented_history_start_utc": iso(VIKE_HISTORY_START),
              "checks": []}
    for tf in timeframes:
        step = timedelta(minutes=MINUTES[tf])
        old_start = old_end - step * 4
        recent_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        recent_end -= timedelta(minutes=recent_end.minute % MINUTES[tf])
        recent_start = recent_end - step * 4
        old_rows = fetch_chunk(symbol, tf, old_start, old_end, key)
        recent_rows = fetch_chunk(symbol, tf, recent_start, recent_end, key)
        report["checks"].append({
            "symbol": symbol, "timeframe": tf,
            "old_window": {"start": iso(old_start), "end": iso(old_end), "candles": len(old_rows)},
            "recent_window": {"start": iso(recent_start), "end": iso(recent_end), "candles": len(recent_rows)},
            "old_window_available": bool(old_rows),
            "recent_window_available": bool(recent_rows),
        })
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--end", default=iso(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)))
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--timeframes", default="15m,1h,4h,1d")
    p.add_argument("--output-dir", default="data/strategy-research/hyperliquid-4y")
    p.add_argument("--chunk-days", type=int, default=28)
    p.add_argument("--preflight-only", action="store_true")
    args = p.parse_args()
    key = os.environ.get("VIKE_API_KEY", "").strip()
    if not key:
        p.error("VIKE_API_KEY is required; never paste or commit it")
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    timeframes = [x.strip() for x in args.timeframes.split(",") if x.strip()]
    if not symbols or not timeframes or args.chunk_days < 1:
        p.error("symbols/timeframes must be non-empty and chunk-days positive")
    if any(tf not in HORIZONS for tf in timeframes):
        raise ValueError(f"Unsupported timeframe; choose {sorted(HORIZONS)}")

    if args.preflight_only:
        result = preflight(symbols[0], timeframes, key)
        print(json.dumps(result, indent=2), flush=True)
        return

    end = utc(args.end)
    # Fail before making dozens of API calls or caching empty chunks.
    unsupported = []
    for tf in timeframes:
        start = end - timedelta(days=HORIZONS[tf])
        if start < VIKE_HISTORY_START:
            unsupported.append({"timeframe": tf, "requested_start": iso(start),
                                "source_start": iso(VIKE_HISTORY_START)})
    if unsupported:
        raise HistoryUnavailable(
            "Vike cannot supply the requested multi-year windows. Its documented "
            "Hyperliquid time-bar history begins 2026-01-01. Run --preflight-only "
            "to verify the live endpoint; do not treat this source as a 2-4 year archive. "
            + json.dumps(unsupported)
        )

    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for symbol in symbols:
        for tf in timeframes:
            start = end - timedelta(days=HORIZONS[tf])
            interval = timedelta(minutes=MINUTES[tf])
            fetch_start = start - interval * 200
            cache_dir = root / symbol / tf / "chunks"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cursor = fetch_start
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=args.chunk_days), end)
                chunk_file = cache_dir / f"{cursor.strftime('%Y%m%dT%H%M%S')}_{chunk_end.strftime('%Y%m%dT%H%M%S')}.json"
                if chunk_file.exists():
                    cached = json.loads(chunk_file.read_text(encoding="utf-8"))
                    if isinstance(cached, list) and cached:
                        cursor = chunk_end
                        continue
                    chunk_file.unlink()
                print(f"FETCH {symbol} {tf} {iso(cursor)} -> {iso(chunk_end)}", flush=True)
                rows = fetch_chunk(symbol, tf, cursor, chunk_end, key)
                if not rows:
                    print(f"EMPTY {symbol} {tf}; not checkpointed (will retry next run)", flush=True)
                else:
                    tmp = chunk_file.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
                    tmp.replace(chunk_file)
                    print(f"CHECKPOINT {symbol} {tf} candles={len(rows)}", flush=True)
                cursor = chunk_end
            all_rows = []
            for file in sorted(cache_dir.glob("*.json")):
                chunk_rows = json.loads(file.read_text(encoding="utf-8"))
                if isinstance(chunk_rows, list):
                    all_rows.extend(chunk_rows)
            by_ts = {int(row[0]): row for row in all_rows}
            merged = [by_ts[k] for k in sorted(by_ts)]
            report = coverage(merged, tf, start, end)
            result = {"exchange": "hyperliquid", "market": "perp", "symbol": symbol,
                      "timeframe": tf, "history_days": HORIZONS[tf],
                      "source": "Vike Hyperliquid OHLCV export", **report}
            results.append(result)
            (root / f"{symbol}_{tf}.json").write_text(json.dumps({
                "exchange": "hyperliquid", "symbol": symbol, "market_type": "futures",
                "timeframe": tf, "candles": merged, "source": result["source"],
                "coverage": report,
            }, separators=(",", ":")), encoding="utf-8")
            (root / "coverage-report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(f"COVERAGE {symbol} {tf}: {report['coverage_pct']}%", flush=True)


if __name__ == "__main__":
    main()
