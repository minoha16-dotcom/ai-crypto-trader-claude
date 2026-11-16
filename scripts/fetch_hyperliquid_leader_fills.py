#!/usr/bin/env python3
"""Snapshot Hyperliquid public wallet fills/funding for a pre-registered cohort.

The wallet discovery snapshot is sourced from UnderHyper's CC BY 4.0
leaderboard dataset. Public fills and funding are read-only Hyperliquid Info
API requests. This creates a raw provenance snapshot; it does not yet infer
stops or calculate system-normalized R labels.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LEADERBOARD_URL = "https://underhyper.com/data/leaderboard.json"
INFO_URL = "https://api.hyperliquid.xyz/info"
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
MAX_PAGE_SIZE = 2000
MAX_RETAINED_HISTORY = 10000
USER_AGENT = "ai-crypto-trader-research/1.0 (read-only public data)"


def _timestamp_ms(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp or Unix milliseconds")
    if isinstance(value, (int, float)):
        number = int(value)
        if number < 100_000_000_000:
            raise ValueError(f"{field} must be Unix milliseconds")
        return number
    raw = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _iso_utc(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _load_leaderboard(url, timeout):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("leaderboard JSON must be an object")
    as_of = payload.get("as_of")
    if not as_of:
        raise ValueError("leaderboard JSON is missing its as_of snapshot timestamp")
    addresses = extract_wallet_addresses(payload)
    if not addresses:
        raise ValueError("no full 0x wallet addresses found in the leaderboard snapshot")
    return raw, payload, addresses, as_of


def extract_wallet_addresses(payload):
    """Find full public wallet addresses without assuming one JSON row layout."""
    found = []
    seen = set()
    address_keys = {
        "address", "ethaddress", "wallet", "walletaddress",
        "leaderaddress", "useraddress",
    }

    def add(value):
        normalized = value.lower()
        if normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and ADDRESS_RE.fullmatch(key):
                    add(key)
                normalized_key = str(key).replace("_", "").replace("-", "").lower()
                if normalized_key in address_keys and isinstance(item, str) and ADDRESS_RE.fullmatch(item):
                    add(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


def _post_info(body, timeout):
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = Request(
        INFO_URL,
        data=encoded,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Hyperliquid Info API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Hyperliquid Info API connection failed: {exc.reason}") from exc


def _fetch_time_range(
    address,
    request_type,
    start_ms,
    end_ms,
    *,
    timeout,
    pause_seconds,
    max_depth=24,
):
    """Split saturated ranges until each response is below the 2,000-row cap."""
    request_count = 0
    truncated = False
    results = []

    def request_range(start, end, depth):
        nonlocal request_count, truncated
        if pause_seconds > 0 and request_count:
            time.sleep(pause_seconds)
        body = {
            "type": request_type,
            "user": address,
            "startTime": int(start),
            "endTime": int(end),
        }
        response = _post_info(body, timeout)
        request_count += 1
        if not isinstance(response, list):
            raise RuntimeError(f"{request_type} returned a non-list response")
        if len(response) < MAX_PAGE_SIZE:
            results.extend(response)
            return
        if depth >= max_depth or end - start <= 1000:
            results.extend(response)
            truncated = True
            return
        middle = (start + end) // 2
        if middle <= start or middle >= end:
            results.extend(response)
            truncated = True
            return
        request_range(start, middle, depth + 1)
        request_range(middle + 1, end, depth + 1)

    request_range(int(start_ms), int(end_ms), 0)
    unique = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("time")
        if timestamp is None:
            continue
        # Hyperliquid fills expose tid. Funding rows expose transaction hash.
        key = (
            str(row.get("tid") or row.get("hash") or ""),
            str(row.get("coin") or (row.get("delta") or {}).get("coin") or ""),
            int(timestamp),
            str(row.get("oid") or ""),
        )
        unique[key] = row
    ordered = sorted(unique.values(), key=lambda row: int(row["time"]))
    cap_reached = len(ordered) >= MAX_RETAINED_HISTORY
    if cap_reached:
        truncated = True
    return ordered, {
        "requests": request_count,
        "rows": len(ordered),
        "response_page_truncated": truncated,
        "history_limit_reached": cap_reached,
        "complete": not truncated and not cap_reached,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Observation start (UTC ISO-8601 or Unix ms)")
    parser.add_argument("--end", required=True, help="Observation end (UTC ISO-8601 or Unix ms)")
    parser.add_argument("--out", default="artifacts/hyperliquid-raw-leader-data.json")
    parser.add_argument("--leaderboard-url", default=LEADERBOARD_URL)
    parser.add_argument("--wallet-limit", type=int, default=200)
    parser.add_argument("--pause-seconds", type=float, default=6.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    start_ms = _timestamp_ms(args.start, "start")
    end_ms = _timestamp_ms(args.end, "end")
    if end_ms <= start_ms:
        raise SystemExit("--end must be later than --start")
    if args.wallet_limit < 1 or args.wallet_limit > 1000:
        raise SystemExit("--wallet-limit must be between 1 and 1000")
    if args.pause_seconds < 0:
        raise SystemExit("--pause-seconds cannot be negative")

    raw_leaderboard, leaderboard, addresses, snapshot_as_of = _load_leaderboard(
        args.leaderboard_url, args.timeout
    )
    snapshot_ms = _timestamp_ms(snapshot_as_of, "leaderboard.as_of")
    if start_ms <= snapshot_ms:
        raise SystemExit(
            "The observation start must be strictly later than the leader-selection "
            "snapshot. Historical backfills from a later-selected winner cohort are "
            "look-ahead-biased and cannot be approved."
        )

    selected = addresses[:args.wallet_limit]
    wallets = []
    for index, address in enumerate(selected, start=1):
        try:
            fills, fill_status = _fetch_time_range(
                address,
                "userFillsByTime",
                start_ms,
                end_ms,
                timeout=args.timeout,
                pause_seconds=args.pause_seconds,
            )
            funding, funding_status = _fetch_time_range(
                address,
                "userFunding",
                start_ms,
                end_ms,
                timeout=args.timeout,
                pause_seconds=args.pause_seconds,
            )
            complete = fill_status["complete"] and funding_status["complete"]
            wallets.append({
                "address": address,
                "fills": fills,
                "funding": funding,
                "complete": complete,
                "fill_history": fill_status,
                "funding_history": funding_status,
            })
            print(
                f"{index}/{len(selected)} {address}: "
                f"{len(fills)} fills, {len(funding)} funding rows, "
                f"{'COMPLETE' if complete else 'INCOMPLETE'}"
            )
        except Exception as exc:
            wallets.append({
                "address": address,
                "fills": [],
                "funding": [],
                "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"{index}/{len(selected)} {address}: INCOMPLETE ({exc})")

    source_hash = hashlib.sha256(raw_leaderboard).hexdigest()
    complete_wallets = sum(1 for row in wallets if row["complete"])
    report = {
        "dataset_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": {
            "source_provider": "UnderHyper leaderboard + Hyperliquid public Info API",
            "source_url": args.leaderboard_url,
            "source_license": "CC BY 4.0; attribute UnderHyper (underhyper.com)",
            "source_snapshot_utc": _iso_utc(snapshot_ms),
            "source_snapshot_sha256": source_hash,
            "cohort_selection_as_of_utc": _iso_utc(snapshot_ms),
            "data_start_utc": _iso_utc(start_ms),
            "data_end_utc": _iso_utc(end_ms),
            "fills_endpoint": INFO_URL,
            "fills_request_type": "userFillsByTime",
            "funding_request_type": "userFunding",
            "max_fills_per_response": MAX_PAGE_SIZE,
            "max_recent_fills_per_wallet": MAX_RETAINED_HISTORY,
            "wallets_selected": len(selected),
            "wallets_complete": complete_wallets,
            "wallets_incomplete": len(selected) - complete_wallets,
            "read_only_public_data": True,
            "attribution": "UnderHyper (underhyper.com), CC BY 4.0",
        },
        "wallets": wallets,
        "limitations": [
            "This is a raw fills/funding snapshot, not a model-ready dataset.",
            "Position reconstruction, candle features and explicit system-normalized R labels are required before training.",
            "Incomplete or history-capped wallets must not contribute eligible training examples.",
            "Hyperliquid's public fills do not reliably reveal each trader's original stop-risk distance.",
        ],
    }
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_bytes = (
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    output_path.write_bytes(output_bytes)
    print(f"OUTPUT {output_path}")
    print(f"DATASET_SHA256 {hashlib.sha256(output_bytes).hexdigest()}")
    print(f"WALLETS_COMPLETE {complete_wallets}/{len(selected)}")
    return 0 if complete_wallets else 2


if __name__ == "__main__":
    raise SystemExit(main())
