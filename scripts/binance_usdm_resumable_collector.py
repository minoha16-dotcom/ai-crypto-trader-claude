#!/usr/bin/env python3
"""Resumable, research-only Binance USD-M historical market-data collector.

Downloads public 15m trade klines, mark-price klines and funding events.
Raw files are CSV plus an atomic checkpoint; missing observations are never
filled. This tool does not create labels, train models, or place orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://fapi.binance.com"
BAR_MS = 15 * 60 * 1000
STREAMS = {
    "trade_15m": ("/fapi/v1/klines", "bar", 1500),
    "mark_15m": ("/fapi/v1/markPriceKlines", "bar", 1500),
    "funding": ("/fapi/v1/fundingRate", "funding", 1000),
}
HEADERS = {
    "trade_15m": ["open_time", "open", "high", "low", "close", "volume",
                  "close_time", "quote_volume", "trade_count", "taker_buy_base",
                  "taker_buy_quote", "ignore"],
    "mark_15m": ["open_time", "open", "high", "low", "close", "volume",\n                  "close_time", "quote_volume", "trade_count", "taker_buy_base",\n                  "taker_buy_quote", "ignore"],
    "funding": ["funding_time", "funding_rate", "mark_price"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_session() -> requests.Session:
    retry = Retry(total=5, connect=5, read=5, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(("GET",)),
                  respect_retry_after_header=True)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "vike-v31-resumable-research-collector/1.0"
    return session


def request_rows(session: requests.Session, endpoint: str, params: dict[str, Any]) -> list:
    response = session.get(BASE_URL + endpoint, params=params, timeout=(10, 45))
    if response.status_code in (418, 429):
        raise RuntimeError(f"Binance rate-limit/ban HTTP {response.status_code}; stop and resume later")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Binance response: {payload!r}")
    return payload


def load_checkpoint(path: Path, symbol: str, start_ms: int, end_ms: int) -> dict:
    if not path.exists():
        return {"schema_version": 1, "symbol": symbol, "start_ms": start_ms,
                "end_ms_exclusive": end_ms, "streams": {}, "updated_at": utc_now()}
    state = json.loads(path.read_text(encoding="utf-8"))
    expected = (symbol, start_ms, end_ms)
    actual = (state.get("symbol"), state.get("start_ms"), state.get("end_ms_exclusive"))
    if actual != expected:
        raise ValueError(f"Checkpoint scope mismatch: expected {expected}, found {actual}")
    if state.get("schema_version") != 1:
        raise ValueError("Unsupported checkpoint schema_version")
    return state


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _row_timestamp(row: Any, kind: str) -> int:
    if kind == "funding":
        if not isinstance(row, dict) or "fundingTime" not in row:
            raise ValueError("Funding response lacks fundingTime")
        return int(row["fundingTime"])
    if not isinstance(row, list) or not row:
        raise ValueError("Kline response row is malformed")
    return int(row[0])


def _csv_values(row: Any, kind: str) -> list:
    if kind == "funding":
        return [row.get("fundingTime"), row.get("fundingRate"), row.get("markPrice")]
    if kind == "bar" and len(row) >= 6:
        if kind == "bar" and len(row) == 6:
            return row[:6]
        return row[:12]
    raise ValueError("Unexpected kline row width")


def _next_cursor(last_timestamp: int, kind: str) -> int:
    # Funding timestamps can carry a +1ms offset; advance by one ms, never
    # assume exact 8-hour multiples. Klines advance by one full bar.
    return last_timestamp + (1 if kind == "funding" else BAR_MS)


def collect_stream(session: requests.Session, out_dir: Path, symbol: str,
                   stream: str, start_ms: int, end_ms: int,
                   state: dict, delay: float = 0.15) -> dict:
    endpoint, kind, limit = STREAMS[stream]
    stream_state = state.setdefault("streams", {}).setdefault(
        stream, {"next_start_ms": start_ms, "rows_written": 0, "complete": False})
    if stream_state.get("complete"):
        return stream_state

    path = out_dir / f"{symbol}-{stream}.csv"
    exists = path.exists() and path.stat().st_size > 0
    mode = "a" if exists else "w"
    cursor = int(stream_state["next_start_ms"])
    with path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(HEADERS[stream])
        while cursor < end_ms:
            params = {"symbol": symbol, "startTime": cursor,
                      "endTime": end_ms - 1, "limit": limit}
            rows = request_rows(session, endpoint, params)
            if not rows:
                # An empty page is not proof of complete coverage. Record the
                # unresolved tail and stop rather than silently skipping it.
                stream_state["empty_page_at_ms"] = cursor
                break
            timestamps = [_row_timestamp(row, kind) for row in rows]
            if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
                raise ValueError(f"{symbol} {stream}: API page is not strictly ordered/unique")
            if timestamps[0] < cursor or timestamps[-1] >= end_ms:
                raise ValueError(f"{symbol} {stream}: API returned rows outside requested window")
            for row in rows:
                writer.writerow(_csv_values(row, kind))
            handle.flush()
            os.fsync(handle.fileno())
            stream_state["rows_written"] += len(rows)
            next_cursor = _next_cursor(timestamps[-1], kind)
            if next_cursor <= cursor:
                raise RuntimeError(f"{symbol} {stream}: pagination made no progress")
            cursor = next_cursor
            stream_state["next_start_ms"] = cursor
            stream_state.pop("empty_page_at_ms", None)
            state["updated_at"] = utc_now()
            atomic_json(out_dir / f"{symbol}-checkpoint.json", state)
            if len(rows) < limit:
                # Do not infer there can be no later records from a short page:
                # continue until the endpoint returns an empty page or end.
                pass
            time.sleep(delay)

    stream_state["complete"] = cursor >= end_ms
    stream_state["next_start_ms"] = cursor
    stream_state["file"] = path.name
    stream_state["sha256"] = sha256_file(path)
    state["updated_at"] = utc_now()
    atomic_json(out_dir / f"{symbol}-checkpoint.json", state)
    return stream_state


def parse_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Dates must include UTC offset or Z")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="Comma-separated USD-M perpetual symbols")
    parser.add_argument("--start", required=True, help="Inclusive ISO-8601 UTC timestamp")
    parser.add_argument("--end", required=True, help="Exclusive ISO-8601 UTC timestamp")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--delay", type=float, default=0.15)
    args = parser.parse_args()
    start_ms, end_ms = parse_ms(args.start), parse_ms(args.end)
    if end_ms <= start_ms:
        parser.error("--end must be later than --start")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols or any(not item.isalnum() for item in symbols):
        parser.error("--symbols must be comma-separated alphanumeric symbols")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    summary = {}
    for symbol in symbols:
        checkpoint = args.out_dir / f"{symbol}-checkpoint.json"
        state = load_checkpoint(checkpoint, symbol, start_ms, end_ms)
        summary[symbol] = {}
        for stream in STREAMS:
            summary[symbol][stream] = collect_stream(
                session, args.out_dir, symbol, stream, start_ms, end_ms, state, args.delay)
        atomic_json(checkpoint, state)
    print(json.dumps({"generated_at": utc_now(), "output_dir": str(args.out_dir),
                      "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
