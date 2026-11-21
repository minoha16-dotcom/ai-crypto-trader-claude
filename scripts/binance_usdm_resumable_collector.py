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
CHECKPOINT_SCHEMA_VERSION = 3
STREAMS = {
    "trade_15m": ("/fapi/v1/klines", "trade_bar", 1500),
    "mark_15m": ("/fapi/v1/markPriceKlines", "mark_bar", 1500),
    "funding": ("/fapi/v1/fundingRate", "funding", 1000),
}
BAR_HEADERS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trade_count",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
# Binance markPriceKlines returns 12 tuple positions, but positions 5 and
# 7-11 are explicitly ignored fields (not trade volume or trade statistics).
# Preserve them losslessly under neutral names rather than mislabeling them.
MARK_HEADERS = [
    "open_time", "mark_open", "mark_high", "mark_low", "mark_close",
    "ignored_5", "close_time", "ignored_7", "ignored_8",
    "ignored_9", "ignored_10", "ignored_11",
]
HEADERS = {
    "trade_15m": BAR_HEADERS,
    "mark_15m": MARK_HEADERS,
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
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "vike-v31-resumable-research-collector/1.0"
    return session


def request_rows(
    session: requests.Session, endpoint: str, params: dict[str, Any]
) -> list:
    response = session.get(BASE_URL + endpoint, params=params, timeout=(10, 45))
    if response.status_code in (418, 429):
        raise RuntimeError(
            f"Binance rate-limit/ban HTTP {response.status_code}; stop and resume later"
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Binance response: {payload!r}")
    return payload


def load_checkpoint(path: Path, symbol: str, start_ms: int, end_ms: int) -> dict:
    if not path.exists():
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "symbol": symbol,
            "start_ms": start_ms,
            "end_ms_exclusive": end_ms,
            "streams": {},
            "updated_at": utc_now(),
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    expected = (symbol, start_ms, end_ms)
    actual = (
        state.get("symbol"),
        state.get("start_ms"),
        state.get("end_ms_exclusive"),
    )
    if actual != expected:
        raise ValueError(
            f"Checkpoint scope mismatch: expected {expected}, found {actual}"
        )
    if state.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported checkpoint schema_version; use a new output directory "
            "or explicitly migrate and audit the old dataset"
        )
    if not isinstance(state.get("streams"), dict):
        raise ValueError("Checkpoint streams must be an object")
    return state


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _row_timestamp(row: Any, kind: str) -> int:
    if kind == "funding":
        if not isinstance(row, dict) or "fundingTime" not in row:
            raise ValueError("Funding response lacks fundingTime")
        return int(row["fundingTime"])
    if kind in ("trade_bar", "mark_bar", "bar"):
        if not isinstance(row, list) or not row:
            raise ValueError("Kline response row is malformed")
        return int(row[0])
    return int(row[0])


def _csv_values(row: Any, kind: str) -> list:
    if kind == "funding":
        if not isinstance(row, dict):
            raise ValueError("Funding response row must be an object")
        required = ("fundingTime", "fundingRate", "markPrice")
        missing = [field for field in required if field not in row]
        if missing:
            raise ValueError(f"Funding response lacks fields: {', '.join(missing)}")
        return [row[field] for field in required]
    if kind in ("trade_bar", "mark_bar", "bar"):
        expected_headers = MARK_HEADERS if kind == "mark_bar" else BAR_HEADERS
        if not isinstance(row, list) or len(row) != len(expected_headers):
            width = len(row) if isinstance(row, list) else "non-list"
            raise ValueError(
                f"{kind} response must contain exactly {len(expected_headers)} fields; got {width}"
            )
        # Mark Price unused tuple positions stay neutral; never call them volume or trade stats.
        return list(row)
    raise ValueError(f"Unsupported row kind: {kind}")


def _next_cursor(last_timestamp: int, kind: str) -> int:
    # Funding timestamps can carry a +1ms offset; advance by one ms, never
    # assume exact 8-hour multiples. Klines advance by one full bar.
    if kind == "funding":
        return last_timestamp + 1
    if kind in ("trade_bar", "mark_bar", "bar"):
        return last_timestamp + BAR_MS
    raise ValueError(f"Unsupported stream kind: {kind}")


def _recover_csv(path: Path, stream: str, stream_state: dict) -> None:
    """Reconcile the CSV to the last atomically committed checkpoint.

    A crash after fsync(CSV) but before checkpoint replacement leaves extra
    bytes. Those bytes are discarded and fetched again. A CSV shorter than the
    checkpoint, or one whose committed content disagrees with it, fails closed.
    """
    if not path.exists():
        raise ValueError(f"Checkpoint exists but CSV is missing: {path}")
    committed_bytes = stream_state.get("committed_bytes")
    expected_rows = stream_state.get("rows_written")
    if not isinstance(committed_bytes, int) or committed_bytes <= 0:
        raise ValueError(f"{stream}: checkpoint lacks a valid committed_bytes")
    if not isinstance(expected_rows, int) or expected_rows < 0:
        raise ValueError(f"{stream}: checkpoint lacks a valid rows_written")
    actual_bytes = path.stat().st_size
    if actual_bytes < committed_bytes:
        raise ValueError(
            f"{stream}: CSV is shorter than its checkpoint "
            f"({actual_bytes} < {committed_bytes} bytes)"
        )
    if actual_bytes > committed_bytes:
        with path.open("r+b") as handle:
            handle.truncate(committed_bytes)
            handle.flush()
            os.fsync(handle.fileno())

    expected_header = HEADERS[stream]
    previous_timestamp = None
    count = 0
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{stream}: CSV is empty") from exc
        if header != expected_header:
            raise ValueError(f"{stream}: CSV header does not match the expected schema")
        for row in reader:
            if len(row) != len(expected_header):
                raise ValueError(
                    f"{stream}: committed CSV row {count + 2} has {len(row)} "
                    f"columns; expected {len(expected_header)}"
                )
            timestamp = int(row[0])
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ValueError(f"{stream}: committed CSV timestamps are not unique/increasing")
            previous_timestamp = timestamp
            count += 1

    if count != expected_rows:
        raise ValueError(
            f"{stream}: CSV row count {count} disagrees with checkpoint {expected_rows}"
        )
    last_timestamp = stream_state.get("last_timestamp_ms")
    if count == 0 and last_timestamp is not None:
        raise ValueError(f"{stream}: empty CSV has a checkpoint last timestamp")
    if count > 0 and previous_timestamp != last_timestamp:
        raise ValueError(f"{stream}: CSV last timestamp disagrees with checkpoint")
    expected_sha256 = stream_state.get("sha256")
    if not expected_sha256 or sha256_file(path) != expected_sha256:
        raise ValueError(f"{stream}: CSV checksum disagrees with checkpoint")


def _initialize_stream(path: Path, stream: str, start_ms: int) -> dict:
    if path.exists():
        # Recover the narrow crash window where the header was durably created
        # but the first checkpoint replacement had not yet happened.
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ValueError(f"{stream}: orphan CSV is empty: {path}") from exc
            if header != HEADERS[stream]:
                raise ValueError(f"{stream}: orphan CSV header does not match expected schema")
            if next(reader, None) is not None:
                raise ValueError(
                    f"CSV contains data without a matching checkpoint: {path}. "
                    "Refusing to guess which rows are committed; use a clean output directory."
                )
        committed_bytes = path.stat().st_size
    else:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADERS[stream])
            handle.flush()
            os.fsync(handle.fileno())
        committed_bytes = path.stat().st_size
    return {
        "next_start_ms": start_ms,
        "rows_written": 0,
        "last_timestamp_ms": None,
        "committed_bytes": committed_bytes,
        "sha256": sha256_file(path),
        "complete": False,
    }


def collect_stream(
    session: requests.Session,
    out_dir: Path,
    symbol: str,
    stream: str,
    start_ms: int,
    end_ms: int,
    state: dict,
    delay: float = 0.15,
) -> dict:
    if stream not in STREAMS:
        raise ValueError(f"Unknown stream: {stream}")
    endpoint, kind, limit = STREAMS[stream]
    streams_state = state.setdefault("streams", {})
    path = out_dir / f"{symbol}-{stream}.csv"

    if stream not in streams_state:
        stream_state = _initialize_stream(path, stream, start_ms)
        streams_state[stream] = stream_state
        state["updated_at"] = utc_now()
        atomic_json(out_dir / f"{symbol}-checkpoint.json", state)
    else:
        stream_state = streams_state[stream]
        _recover_csv(path, stream, stream_state)

    if stream_state.get("complete"):
        return stream_state

    cursor = int(stream_state["next_start_ms"])
    if cursor < start_ms or cursor > end_ms:
        raise ValueError(f"{symbol} {stream}: checkpoint cursor is outside requested window")

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        while cursor < end_ms:
            params = {
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": limit,
            }
            if kind in ("trade_bar", "mark_bar", "bar"):
                params["interval"] = "15m"
            rows = request_rows(session, endpoint, params)
            if not rows:
                stream_state["empty_page_at_ms"] = cursor
                if kind == "funding":
                    # The bounded funding query has no later rows. This marks
                    # pagination exhaustion only; a separate audit must verify
                    # historical funding coverage before training.
                    stream_state["complete"] = True
                    stream_state["termination_reason"] = "api_empty_page"
                    stream_state["coverage_verified"] = False
                state["updated_at"] = utc_now()
                atomic_json(out_dir / f"{symbol}-checkpoint.json", state)
                break
            if len(rows) > limit:
                raise ValueError(f"{symbol} {stream}: API page exceeds requested limit")

            timestamps = [_row_timestamp(row, kind) for row in rows]
            if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
                raise ValueError(f"{symbol} {stream}: API page is not strictly ordered/unique")
            if timestamps[0] < cursor or timestamps[-1] >= end_ms:
                raise ValueError(f"{symbol} {stream}: API returned rows outside requested window")
            if stream_state["last_timestamp_ms"] is not None:
                if timestamps[0] <= int(stream_state["last_timestamp_ms"]):
                    raise ValueError(f"{symbol} {stream}: API page overlaps committed CSV rows")

            csv_rows = [_csv_values(row, kind) for row in rows]
            for values in csv_rows:
                writer.writerow(values)
            handle.flush()
            os.fsync(handle.fileno())

            # Commit metadata only after the CSV page is durable. If this
            # checkpoint write fails or the process dies, recovery truncates
            # the uncommitted CSV tail to the previous committed byte offset.
            stream_state["rows_written"] += len(csv_rows)
            stream_state["last_timestamp_ms"] = timestamps[-1]
            stream_state["committed_bytes"] = path.stat().st_size
            stream_state["sha256"] = sha256_file(path)
            cursor = _next_cursor(timestamps[-1], kind)
            if cursor <= int(stream_state["next_start_ms"]):
                raise RuntimeError(f"{symbol} {stream}: pagination made no progress")
            stream_state["next_start_ms"] = cursor
            stream_state.pop("empty_page_at_ms", None)
            state["updated_at"] = utc_now()
            atomic_json(out_dir / f"{symbol}-checkpoint.json", state)
            time.sleep(delay)

    if cursor >= end_ms:
        stream_state["complete"] = True
        stream_state["termination_reason"] = "requested_window_end"
        if kind == "funding":
            stream_state["coverage_verified"] = False
    elif kind != "funding":
        stream_state["complete"] = False
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
    if len(symbols) != len(set(symbols)):
        parser.error("--symbols contains duplicates")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    summary = {}
    for symbol in symbols:
        checkpoint = args.out_dir / f"{symbol}-checkpoint.json"
        state = load_checkpoint(checkpoint, symbol, start_ms, end_ms)
        summary[symbol] = {}
        for stream in STREAMS:
            summary[symbol][stream] = collect_stream(
                session, args.out_dir, symbol, stream, start_ms, end_ms, state, args.delay
            )
        atomic_json(checkpoint, state)
    print(
        json.dumps(
            {"generated_at": utc_now(), "output_dir": str(args.out_dir), "summary": summary},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
