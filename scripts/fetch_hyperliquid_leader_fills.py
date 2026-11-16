#!/usr/bin/env python3
"""Snapshot Hyperliquid's public leaderboard and selected wallets' public fills.

The cohort is ranked locally from Hyperliquid's public leaderboard file by its
month PnL metric. UnderHyper's dated CC BY 4.0 metadata is retained as a
third-party citation, but that license is not assumed to cover the raw
Hyperliquid source data. This output is raw evidence, not model-ready training
data.
"""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LEADERBOARD_METADATA_URL = "https://underhyper.com/data/leaderboard.json"
INFO_URL = "https://api.hyperliquid.xyz/info"
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
MAX_PAGE_SIZE_BY_TYPE = {"userFillsByTime": 2000, "userFunding": 500}
MAX_RETAINED_HISTORY = 10000
MAX_HISTORICAL_ORDERS = 2000
INFO_WEIGHT_BUDGET_PER_MINUTE = 900
INFO_BASE_REQUEST_WEIGHT = 20
USER_AGENT = "ai-crypto-trader-research/1.1 (read-only public data)"


def _timestamp_ms(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp or Unix milliseconds")
    if isinstance(value, (int, float)):
        number = float(value)
        if not number.is_integer() or number < 100_000_000_000:
            raise ValueError(f"{field} must be integer Unix milliseconds")
        return int(number)
    raw = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _iso_utc(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _cohort_selection_cutoff_ms(source_snapshot_ms, leaderboard_retrieved_ms):
    """Use actual retrieval/selection time to prevent post-snapshot look-ahead."""
    return max(int(source_snapshot_ms), int(leaderboard_retrieved_ms))


def _get_json(url, timeout):
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{url} must return a JSON object")
    return raw, payload


def _window_performance(row):
    """Normalize Hyperliquid's windowPerformances list-of-pairs to a mapping."""
    raw = row.get("windowPerformances")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        raise ValueError("leaderboard row is missing windowPerformances")
    normalized = {}
    for item in raw:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], dict)
        ):
            normalized[item[0]] = item[1]
    return normalized


def extract_wallet_addresses(payload, cohort="top_pnl"):
    """Rank Hyperliquid leaderboardRows by positive month PnL or month volume.

    Accounts with zero month volume are excluded, so account-value changes are
    not mistaken for evidence of executed trading activity.
    """
    if cohort not in ("top_pnl", "top_volume"):
        raise ValueError("cohort must be top_pnl or top_volume")
    rows = payload.get("leaderboardRows")
    if not isinstance(rows, list) or len(rows) < 100:
        raise ValueError(
            "Hyperliquid leaderboard schema is missing or unexpectedly small; "
            "refusing to select an unverified cohort"
        )

    ranked = []
    seen = set()
    for source_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        address = row.get("ethAddress")
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
            continue
        address = address.lower()
        if address in seen:
            continue
        seen.add(address)
        try:
            month = _window_performance(row).get("month") or {}
            pnl = float(month.get("pnl"))
            volume = float(month.get("vlm"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(pnl) or not math.isfinite(volume) or volume <= 0:
            continue
        metric = pnl if cohort == "top_pnl" else volume
        if cohort == "top_pnl" and pnl <= 0:
            continue
        ranked.append((metric, source_index, address))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    if not ranked:
        raise ValueError(f"no eligible wallet addresses found for {cohort}")
    return [address for _, _, address in ranked]


def _load_leaderboard(
    leaderboard_url,
    metadata_url,
    timeout,
    cohort="top_pnl",
):
    """Fetch official rows and a separately timestamped/attributed metadata record."""
    raw_metadata, metadata = _get_json(metadata_url, timeout)
    as_of = metadata.get("as_of")
    if not as_of:
        raise ValueError("UnderHyper metadata is missing its as_of snapshot timestamp")
    _timestamp_ms(as_of, "leaderboard metadata as_of")

    raw_leaderboard, leaderboard = _get_json(leaderboard_url, timeout)
    addresses = extract_wallet_addresses(leaderboard, cohort=cohort)
    return raw_leaderboard, raw_metadata, metadata, addresses, as_of


def _info_request_weight(response):
    """Conservative estimate: base request weight plus one per 20 returned rows."""
    if isinstance(response, list):
        return INFO_BASE_REQUEST_WEIGHT + math.ceil(len(response) / 20)
    return INFO_BASE_REQUEST_WEIGHT


def _weighted_pacing_delay(response, budget_per_minute=INFO_WEIGHT_BUDGET_PER_MINUTE):
    if budget_per_minute <= 0:
        raise ValueError("budget_per_minute must be positive")
    return _info_request_weight(response) * 60.0 / float(budget_per_minute)


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
    for attempt in range(4):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            # Hyperliquid documents a 1,200 weight/minute per-IP limit. Keep
            # collection below that ceiling to leave room for other API use.
            time.sleep(_weighted_pacing_delay(payload))
            return payload
        except HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            raise RuntimeError(f"Hyperliquid Info API returned HTTP {exc.code}") from exc
        except URLError as exc:
            if attempt < 3:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            raise RuntimeError(f"Hyperliquid Info API connection failed: {exc.reason}") from exc
    raise RuntimeError("Hyperliquid Info API exhausted retry attempts")


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
    """Split saturated ranges below each endpoint's page cap."""
    request_count = 0
    truncated = False
    results = []

    def request_range(start, end, depth):
        nonlocal request_count, truncated
        if pause_seconds > 0 and request_count:
            time.sleep(pause_seconds)
        response = _post_info({
            "type": request_type,
            "user": address,
            "startTime": int(start),
            "endTime": int(end),
        }, timeout)
        request_count += 1
        if not isinstance(response, list):
            raise RuntimeError(f"{request_type} returned a non-list response")
        page_size = MAX_PAGE_SIZE_BY_TYPE.get(request_type, 500)
        if len(response) < page_size:
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
        if not isinstance(row, dict) or row.get("time") is None:
            continue
        nested = row.get("delta") or {}
        trade_id = row.get("tid")
        if trade_id is not None:
            identity = f"tid:{trade_id}"
        else:
            # Transaction hash alone is not a fill identifier: one L1 transaction
            # can contain multiple fills. Include the complete economic payload.
            identity = "fallback:" + hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        key = (
            identity,
            str(row.get("coin") or nested.get("coin") or ""),
            int(row["time"]),
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


def _fetch_historical_orders(address, *, timeout):
    """Fetch the venue's recent order audit needed to classify trigger exits.

    Hyperliquid returns at most 2,000 historical order records. If exactly
    2,000 rows are returned, completeness cannot be established and the wallet
    is fail-closed for training preparation.
    """
    response = _post_info(
        {"type": "historicalOrders", "user": address},
        timeout,
    )
    if not isinstance(response, list):
        raise RuntimeError("historicalOrders returned a non-list response")
    rows = [row for row in response if isinstance(row, dict)]
    truncated = len(rows) >= MAX_HISTORICAL_ORDERS
    return rows, {
        "rows": len(rows),
        "history_limit_reached": truncated,
        "complete_within_documented_limit": not truncated,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Observation start (UTC ISO-8601 or Unix ms)")
    parser.add_argument("--end", required=True, help="Observation end (UTC ISO-8601 or Unix ms)")
    parser.add_argument("--out", default="artifacts/hyperliquid-raw-leader-data.json")
    parser.add_argument("--leaderboard-url", default=LEADERBOARD_URL)
    parser.add_argument("--metadata-url", default=LEADERBOARD_METADATA_URL)
    parser.add_argument(
        "--cohort",
        choices=("top_pnl", "top_volume"),
        default="top_pnl",
        help="Rank the published month PnL window by default; volume is an explicit separate cohort.",
    )
    parser.add_argument("--wallet-limit", type=int, default=100)
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    start_ms = _timestamp_ms(args.start, "start")
    end_ms = _timestamp_ms(args.end, "end")
    if end_ms <= start_ms:
        raise SystemExit("--end must be later than --start")
    if args.wallet_limit < 1 or args.wallet_limit > 100:
        raise SystemExit("--wallet-limit must be between 1 and 100")
    if args.pause_seconds < 0:
        raise SystemExit("--pause-seconds cannot be negative")

    (
        raw_leaderboard, raw_metadata, metadata, addresses, snapshot_as_of
    ) = _load_leaderboard(
        args.leaderboard_url,
        args.metadata_url,
        args.timeout,
        cohort=args.cohort,
    )
    snapshot_ms = _timestamp_ms(snapshot_as_of, "leaderboard.as_of")
    retrieved_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cohort_selection_ms = _cohort_selection_cutoff_ms(snapshot_ms, retrieved_at_ms)
    if start_ms <= cohort_selection_ms:
        raise SystemExit(
            "The observation start must be strictly later than the actual cohort "
            "selection/download time. Historical backfills from a later-selected "
            "winner cohort are look-ahead-biased and cannot be approved."
        )

    selected = addresses[:args.wallet_limit]
    wallets = []
    for index, address in enumerate(selected, start=1):
        try:
            fills, fill_status = _fetch_time_range(
                address, "userFillsByTime", start_ms, end_ms,
                timeout=args.timeout, pause_seconds=args.pause_seconds,
            )
            funding, funding_status = _fetch_time_range(
                address, "userFunding", start_ms, end_ms,
                timeout=args.timeout, pause_seconds=args.pause_seconds,
            )
            if args.pause_seconds > 0:
                time.sleep(args.pause_seconds)
            historical_orders, orders_status = _fetch_historical_orders(
                address, timeout=args.timeout
            )
            complete = (
                fill_status["complete"]
                and funding_status["complete"]
                and orders_status["complete_within_documented_limit"]
            )
            wallets.append({
                "address": address,
                "rank": index,
                "fills": fills,
                "funding": funding,
                "historical_orders": historical_orders,
                "complete": complete,
                "training_eligible": False,
                "fill_history": fill_status,
                "funding_history": funding_status,
                "historical_orders_status": orders_status,
            })
            print(
                f"{index}/{len(selected)} {address}: "
                f"{len(fills)} fills, {len(funding)} funding rows, "
                f"{len(historical_orders)} historical orders, "
                f"{'COMPLETE_RAW_SNAPSHOT' if complete else 'INCOMPLETE'}"
            )
        except Exception as exc:
            wallets.append({
                "address": address,
                "rank": index,
                "fills": [],
                "funding": [],
                "historical_orders": [],
                "complete": False,
                "training_eligible": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"{index}/{len(selected)} {address}: INCOMPLETE ({exc})")

    source_hash = hashlib.sha256(raw_leaderboard).hexdigest()
    metadata_hash = hashlib.sha256(raw_metadata).hexdigest()
    complete_wallets = sum(1 for row in wallets if row["complete"])
    license_note = (
        "Hyperliquid public leaderboard and Info API data; the raw endpoint does not "
        "state a separate CC BY grant. UnderHyper metadata page is CC BY 4.0 with "
        "attribution, but that license is not assumed to cover Hyperliquid raw data."
    )
    report = {
        "dataset_version": 2,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": {
            "source_provider": "Hyperliquid public leaderboard + Info API; UnderHyper snapshot metadata",
            "source_url": args.leaderboard_url,
            "source_metadata_url": args.metadata_url,
            "source_license": license_note,
            "source_snapshot_utc": _iso_utc(snapshot_ms),
            "leaderboard_retrieved_at_utc": _iso_utc(retrieved_at_ms),
            "source_snapshot_sha256": source_hash,
            "source_metadata_sha256": metadata_hash,
            "cohort_selection_as_of_utc": _iso_utc(cohort_selection_ms),
            "data_start_utc": _iso_utc(start_ms),
            "data_end_utc": _iso_utc(end_ms),
            "fills_endpoint": INFO_URL,
            "fills_request_type": "userFillsByTime",
            "funding_request_type": "userFunding",
            "historical_orders_request_type": "historicalOrders",
            "max_historical_orders_per_wallet": MAX_HISTORICAL_ORDERS,
            "max_fills_per_response": MAX_PAGE_SIZE_BY_TYPE["userFillsByTime"],
            "max_funding_rows_per_response": MAX_PAGE_SIZE_BY_TYPE["userFunding"],
            "max_recent_fills_per_wallet": MAX_RETAINED_HISTORY,
            "info_rate_limit_weight_budget_per_minute": INFO_WEIGHT_BUDGET_PER_MINUTE,
            "info_weight_policy": "base request weight 20 plus ceil(response rows/20); response-weighted client pacing below the documented 1200/minute IP ceiling",
            "cohort_selection_method": args.cohort,
            "cohort_metric_window": "month",
            "cohort_accounts_available": len(addresses),
            "wallets_selected": len(selected),
            "wallets_complete_raw_snapshots": complete_wallets,
            "wallets_incomplete_raw_snapshots": len(selected) - complete_wallets,
            "read_only_public_data": True,
            "attribution": "UnderHyper leaderboard metadata, CC BY 4.0; Hyperliquid raw data source separately identified",
            "not_model_ready": True,
        },
        "wallets": wallets,
        "limitations": [
            "This is a raw fills/funding snapshot, not a model-ready dataset.",
            "Position reconstruction, candle features and system-normalized R labels are required before training.",
            "Only fully retrieved raw wallet histories may proceed to a later preparation step.",
            "Hyperliquid userFillsByTime exposes only the 10,000 most recent fills and at most 2,000 fills per response.",
            "Hyperliquid historicalOrders exposes at most 2,000 recent order records per wallet; a full page is treated as incomplete.",
            "The raw leaderboard endpoint has no separate explicit reuse license statement; review terms before redistribution.",
            "The fills endpoint does not state each trader's original stop-risk distance. Model labels must use a declared system-normalized risk unit.",
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
    print(f"WALLETS_COMPLETE_RAW {complete_wallets}/{len(selected)}")
    return 0 if complete_wallets else 2


if __name__ == "__main__":
    raise SystemExit(main())
