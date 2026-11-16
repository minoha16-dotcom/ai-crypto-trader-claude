#!/usr/bin/env python3
"""Prepare a conservative, reproducible Hyperliquid per-market Advisor dataset.

This script does NOT infer a trader's original stop risk. It reconstructs only
single-entry, non-scaled, fully closed linear USDC perp positions whose trigger
exit can be matched to a historical reduce-only order. All other reconstructable
closes remain explicit excluded signal rows. Unreconstructable positions,
incomplete wallet histories, and missing candle windows are counted separately.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from typing import Any

import ccxt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.indicators import analyze
from backend.advisor_model_validation import (
    FEATURE_SCHEMA_VERSION,
    MODEL_VERSION,
)


HOUR_MS = 60 * 60 * 1000
FOUR_HOURS_MS = 4 * HOUR_MS
DEFAULT_TIMEFRAMES = ("1h", "4h")
ELIGIBLE_EXIT_REASONS = {
    "STOP", "STOP_GAP", "TARGET", "TARGET_GAP", "TIME_EXIT",
}
MAX_OHLCV_PAGES = 400
OHLCV_PAGE_SIZE = 500
MIN_FEATURE_CANDLES = 120
FEATURE_CONTIGUOUS_CANDLES = 120
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        int(timestamp_ms) / 1000, tz=timezone.utc
    ).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} is not finite")
    return result


def _fill_direction(fill: dict[str, Any]) -> tuple[str | None, str | None]:
    label = str(fill.get("dir") or "").strip().lower()
    if "open long" in label:
        return "open", "LONG"
    if "add long" in label:
        return "add", "LONG"
    if "close long" in label:
        return "close", "LONG"
    if "open short" in label:
        return "open", "SHORT"
    if "add short" in label:
        return "add", "SHORT"
    if "close short" in label:
        return "close", "SHORT"
    if "liquidat" in label or "auto-deleverag" in label or "adl" in label:
        return "liquidation", None
    return None, None


def _historical_order_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index top-level and nested child orders without inheriting parent status."""
    index = {}

    def visit(raw_order: Any):
        if not isinstance(raw_order, dict):
            return
        nested_order = raw_order.get("order")
        order = nested_order if isinstance(nested_order, dict) else raw_order
        oid = order.get("oid")
        if oid is not None:
            index[str(oid)] = {
                **order,
                "_status": raw_order.get("status") if nested_order is not None else order.get("status"),
                "_status_timestamp": (
                    raw_order.get("statusTimestamp")
                    if nested_order is not None else order.get("statusTimestamp")
                ),
            }
        children = order.get("children") or raw_order.get("children") or []
        if isinstance(children, list):
            for child in children:
                visit(child)

    for row in rows:
        visit(row)
    return index


def _classify_trigger_exit(
    position_side: str,
    exit_price: float,
    close_oid: Any,
    coin: str,
    order_index: dict[str, dict[str, Any]],
) -> str:
    """Classify from explicit trigger type/direction, not entry-price comparison."""
    if close_oid is None:
        return "MANUAL_CLOSE"
    order = order_index.get(str(close_oid))
    if not order:
        return "MANUAL_CLOSE"
    if not (order.get("isTrigger") is True or order.get("isPositionTpsl") is True):
        return "MANUAL_CLOSE"
    if order.get("reduceOnly") is not True:
        return "MANUAL_CLOSE"
    if str(order.get("coin") or "").upper() != str(coin).upper():
        return "UNKNOWN"

    expected_order_side = "A" if position_side == "LONG" else "B" if position_side == "SHORT" else None
    if expected_order_side is None or str(order.get("side") or "").upper() != expected_order_side:
        return "UNKNOWN"

    order_type = str(order.get("orderType") or "").strip().lower()
    if order_type in {"stop market", "stop limit"}:
        label = "STOP"
    elif order_type in {"take market", "take limit", "take profit market", "take profit limit"}:
        label = "TARGET"
    else:
        return "UNKNOWN"

    condition = str(order.get("triggerCondition") or "").strip().lower()
    if condition.startswith("price below"):
        condition_direction = "BELOW"
    elif condition.startswith("price above"):
        condition_direction = "ABOVE"
    else:
        return "UNKNOWN"
    expected_direction = (
        ("BELOW" if label == "STOP" else "ABOVE")
        if position_side == "LONG"
        else ("ABOVE" if label == "STOP" else "BELOW")
    )
    if condition_direction != expected_direction:
        return "UNKNOWN"

    try:
        trigger_price = _finite(order.get("triggerPx"), "triggerPx")
        exit_price = _finite(exit_price, "exit_price")
    except ValueError:
        return "UNKNOWN"
    if trigger_price <= 0 or exit_price <= 0:
        return "UNKNOWN"

    if position_side == "LONG":
        if label == "STOP":
            return "STOP_GAP" if exit_price < trigger_price else "STOP"
        return "TARGET_GAP" if exit_price > trigger_price else "TARGET"
    if label == "STOP":
        return "STOP_GAP" if exit_price > trigger_price else "STOP"
    return "TARGET_GAP" if exit_price < trigger_price else "TARGET"


def _funding_for_position(
    funding_rows: list[dict[str, Any]],
    coin: str,
    entry_ms: int,
    exit_ms: int,
) -> tuple[float, bool]:
    total = 0.0
    for row in funding_rows:
        if not isinstance(row, dict):
            return 0.0, False
        try:
            timestamp = int(row.get("time"))
        except (TypeError, ValueError, OverflowError):
            return 0.0, False
        if timestamp < entry_ms or timestamp > exit_ms:
            continue
        delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
        row_coin = str(row.get("coin") or delta.get("coin") or "").strip()
        if not row_coin:
            return 0.0, False
        if row_coin.upper() != coin.upper():
            continue
        try:
            amount = _finite(delta.get("usdc"), "funding.delta.usdc")
        except ValueError:
            return 0.0, False
        total += amount
    return total, True


def raw_wallet_history_complete(wallet: dict[str, Any]) -> bool:
    """Require explicit completeness flags; an empty verified order list is valid."""
    if not wallet.get("complete"):
        return False
    fills = wallet.get("fill_history") or {}
    funding = wallet.get("funding_history") or {}
    twap = wallet.get("twap_history") or {}
    orders = wallet.get("historical_orders_status") or {}
    return bool(
        fills.get("complete")
        and funding.get("complete")
        and twap.get("complete")
        and orders.get("complete_within_documented_limit")
    )


def reconstruct_closed_positions(wallet: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reconstruct only fully closed single-entry, non-scaled linear positions."""
    reasons: Counter[str] = Counter()
    if not wallet.get("complete"):
        return [], {"RAW_WALLET_INCOMPLETE": len(wallet.get("fills") or []) or 1}
    fills = list(wallet.get("fills") or [])
    for wrapper in wallet.get("twap_slice_fills") or []:
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("fill"), dict):
            return [], {"MALFORMED_TWAP_SLICE_FILL": 1}
        twap_fill = dict(wrapper["fill"])
        twap_fill["twapId"] = wrapper.get("twapId")
        twap_fill["_fill_source"] = "userTwapSliceFills"
        fills.append(twap_fill)
    funding = wallet.get("funding") or []
    orders = _historical_order_index(wallet.get("historical_orders") or [])
    positions = []
    current: dict[str, dict[str, Any]] = {}

    def add_fee(position: dict[str, Any], fill: dict[str, Any]) -> bool:
        try:
            fee = _finite(fill.get("fee"), "fee")
        except ValueError:
            position["invalid_reason"] = position.get("invalid_reason") or "MISSING_OR_INVALID_FEE"
            return False
        token = str(fill.get("feeToken") or "").strip().upper()
        if token not in ("USDC", "USD"):
            position["invalid_reason"] = position.get("invalid_reason") or "UNSUPPORTED_FEE_CURRENCY"
            return False
        position["fees"] += fee
        return True

    ordered_fills = sorted(
        (row for row in fills if isinstance(row, dict)),
        key=lambda row: (int(row.get("time") or 0), str(row.get("tid") or "")),
    )
    for fill in ordered_fills:
        coin = str(fill.get("coin") or "")
        if not coin:
            reasons["MISSING_COIN"] += 1
            continue
        try:
            timestamp = int(fill.get("time"))
            price = _finite(fill.get("px"), "fill.px")
            quantity = _finite(fill.get("sz"), "fill.sz")
        except (ValueError, TypeError, OverflowError):
            reasons["INVALID_FILL_NUMERIC"] += 1
            continue
        if timestamp <= 0 or price <= 0 or quantity <= 0:
            reasons["INVALID_FILL_NUMERIC"] += 1
            continue

        action, side = _fill_direction(fill)
        active = current.get(coin)
        oid = fill.get("oid")
        try:
            start_position = _finite(fill.get("startPosition"), "startPosition")
        except ValueError:
            start_position = None

        start_position_issue = None
        if action == "open" and active is None:
            if start_position is None or abs(start_position) > 1e-8:
                start_position_issue = "OPEN_START_POSITION_NOT_ZERO"
        elif active is not None:
            expected_start = (
                active["remaining_quantity"]
                if active["side"] == "LONG"
                else -active["remaining_quantity"]
            )
            tolerance = max(1e-8, abs(expected_start) * 1e-6)
            if (
                start_position is None
                or abs(start_position - expected_start) > tolerance
            ):
                start_position_issue = "START_POSITION_MISMATCH"

        if action == "open" and side:
            if active is None:
                active = {
                    "coin": coin,
                    "side": side,
                    "entry_price": price,
                    "entry_quantity": quantity,
                    "remaining_quantity": quantity,
                    "entry_timestamp": timestamp,
                    "entry_start_timestamp": timestamp,
                    "open_oid": str(oid) if oid is not None else None,
                    "fees": 0.0,
                    "closed_pnl_reported": 0.0,
                    "gross_pnl_reconstructed": 0.0,
                    "close_quantity": 0.0,
                    "last_close_oid": None,
                    "close_oids": set(),
                    "last_exit_price": None,
                    "exit_timestamp": None,
                    "invalid_reason": start_position_issue,
                    "liquidated": False,
                }
                current[coin] = active
            elif (
                active.get("open_oid")
                and oid is not None
                and str(oid) == active["open_oid"]
                and active["side"] == side
            ):
                # Multiple fills from the same opening order are one entry.
                # A later opening fill after a reduction invalidates this risk path.
                if active.get("close_quantity", 0.0) > 0:
                    active["invalid_reason"] = (
                        active.get("invalid_reason") or "ENTRY_FILL_AFTER_POSITION_REDUCTION"
                    )
                active["entry_timestamp"] = max(active["entry_timestamp"], timestamp)
                new_quantity = active["entry_quantity"] + quantity
                active["entry_price"] = (
                    active["entry_price"] * active["entry_quantity"]
                    + price * quantity
                ) / new_quantity
                active["entry_quantity"] = new_quantity
                active["remaining_quantity"] += quantity
            else:
                active["invalid_reason"] = active.get("invalid_reason") or "MULTIPLE_ENTRY_ORDERS_OR_SCALE_IN"
            if start_position_issue:
                active["invalid_reason"] = active.get("invalid_reason") or start_position_issue
            add_fee(active, fill)
            continue

        if action == "add":
            if active:
                active["invalid_reason"] = active.get("invalid_reason") or start_position_issue or "SCALED_POSITION"
            else:
                reasons["ORPHAN_SCALE_IN"] += 1
            continue

        if action in ("close", "liquidation"):
            if active is None:
                reasons["ORPHAN_CLOSE_OR_LIQUIDATION"] += 1
                continue
            if start_position_issue:
                active["invalid_reason"] = active.get("invalid_reason") or start_position_issue
            if action == "close" and side != active["side"]:
                active["invalid_reason"] = active.get("invalid_reason") or "CLOSE_SIDE_MISMATCH"
            if action == "liquidation":
                active["liquidated"] = True
            if quantity > active["remaining_quantity"] + max(1e-9, active["entry_quantity"] * 1e-7):
                active["invalid_reason"] = active.get("invalid_reason") or "CLOSE_EXCEEDS_OPEN_QUANTITY"
            try:
                exchange_closed_pnl = _finite(fill.get("closedPnl"), "closedPnl")
            except ValueError:
                exchange_closed_pnl = 0.0
            # The server's closedPnl is retained only for audit because public
            # documentation describes it as a frontend convenience field and
            # fee-inclusion semantics differ from the raw fee field.
            active["closed_pnl_reported"] += exchange_closed_pnl
            direction_sign = 1.0 if active["side"] == "LONG" else -1.0
            active["gross_pnl_reconstructed"] += (
                direction_sign * (price - active["entry_price"]) * quantity
            )
            active["close_quantity"] += quantity
            active["remaining_quantity"] = max(0.0, active["remaining_quantity"] - quantity)
            active["last_close_oid"] = oid
            if oid is not None:
                active["close_oids"].add(str(oid))
            active["last_exit_price"] = price
            active["exit_timestamp"] = timestamp
            add_fee(active, fill)

            if active["remaining_quantity"] <= max(1e-9, active["entry_quantity"] * 1e-7):
                active["funding_rows"] = funding
                funding_paid, funding_ok = _funding_for_position(
                    funding,
                    coin,
                    active.get("entry_start_timestamp", active["entry_timestamp"]),
                    active["exit_timestamp"],
                )
                active["funding_paid"] = funding_paid
                if not funding_ok:
                    active["invalid_reason"] = active.get("invalid_reason") or "INVALID_FUNDING_RECORD"
                if active.get("liquidated"):
                    exit_reason = "LIQUIDATION"
                else:
                    if len(active.get("close_oids") or []) != 1:
                        exit_reason = "MANUAL_CLOSE"
                    else:
                        exit_reason = _classify_trigger_exit(
                            active["side"],
                            active["last_exit_price"],
                            active["last_close_oid"],
                            coin,
                            orders,
                        )
                if active.get("invalid_reason"):
                    exit_reason = "UNKNOWN"
                positions.append({
                    **active,
                    "exit_reason": exit_reason,
                })
                current.pop(coin, None)
            continue

        if active is not None:
            active["invalid_reason"] = active.get("invalid_reason") or start_position_issue or "UNRECOGNIZED_FILL_DIRECTION"
        else:
            reasons["UNRECOGNIZED_FILL_DIRECTION"] += 1

    for coin, active in current.items():
        reasons["OPEN_POSITION_AT_END"] += 1
        reasons[active.get("invalid_reason") or "UNRESOLVED_OPEN_POSITION"] += 1

    for position in positions:
        if position.get("invalid_reason"):
            reasons[position["invalid_reason"]] += 1
        elif position["exit_reason"] not in ELIGIBLE_EXIT_REASONS:
            reasons[position["exit_reason"]] += 1

    return positions, dict(sorted(reasons.items()))


def _market_symbol(exchange: Any, coin: str) -> str:
    candidates = [
        market for market in exchange.markets.values()
        if str(market.get("id") or "").upper() == coin.upper()
        and market.get("contract")
        and market.get("swap")
        and market.get("linear")
    ]
    preferred = [
        market for market in candidates
        if str(market.get("settle") or "").upper() in ("USDC", "USD")
    ]
    candidates = preferred or candidates
    if not candidates:
        raise ValueError(f"no supported linear Hyperliquid perp market for {coin}")
    return sorted(candidates, key=lambda row: str(row.get("symbol") or ""))[0]["symbol"]


def fetch_hourly_candles(
    exchange: Any,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float]]:
    """Fetch closed 1h OHLCV with pagination and no synthetic gap filling."""
    cursor = max(0, int(start_ms))
    end_ms = int(end_ms)
    all_rows: dict[int, list[float]] = {}
    for _ in range(MAX_OHLCV_PAGES):
        if cursor >= end_ms:
            break
        batch = exchange.fetch_ohlcv(
            symbol,
            timeframe="1h",
            since=cursor,
            limit=OHLCV_PAGE_SIZE,
        )
        if not batch:
            break
        fresh = []
        for candle in batch:
            if len(candle) < 6:
                continue
            timestamp = int(candle[0])
            if timestamp < cursor or timestamp >= end_ms:
                continue
            all_rows[timestamp] = [
                timestamp,
                float(candle[1]),
                float(candle[2]),
                float(candle[3]),
                float(candle[4]),
                float(candle[5]),
            ]
            fresh.append(timestamp)
        if not fresh:
            break
        next_cursor = max(fresh) + HOUR_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < OHLCV_PAGE_SIZE:
            break
    return [all_rows[key] for key in sorted(all_rows)]


def aggregate_four_hour(hourly: list[list[float]]) -> list[list[float]]:
    buckets: dict[int, dict[int, list[float]]] = defaultdict(dict)
    for candle in hourly:
        timestamp = int(candle[0])
        bucket = (timestamp // FOUR_HOURS_MS) * FOUR_HOURS_MS
        hour_offset = (timestamp - bucket) // HOUR_MS
        buckets[bucket][int(hour_offset)] = candle
    output = []
    for bucket, items in sorted(buckets.items()):
        if set(items) != {0, 1, 2, 3}:
            continue
        rows = [items[index] for index in range(4)]
        output.append([
            bucket,
            rows[0][1],
            max(row[2] for row in rows),
            min(row[3] for row in rows),
            rows[-1][4],
            sum(row[5] for row in rows),
        ])
    return output


def _position_signal_timestamp(position: dict[str, Any]) -> int:
    """Use the first actual opening fill as the signal/feature timestamp."""
    return int(position.get("entry_start_timestamp") or position["entry_timestamp"])


def features_before_entry(
    candles: list[list[float]],
    entry_timestamp: int,
    timeframe: str,
) -> dict[str, float]:
    timeframe_ms = HOUR_MS if timeframe == "1h" else FOUR_HOURS_MS
    closed = [
        row for row in candles
        if int(row[0]) + timeframe_ms <= int(entry_timestamp)
    ]
    if len(closed) < MIN_FEATURE_CANDLES:
        raise ValueError("INSUFFICIENT_PRE_ENTRY_CANDLES")
    closed = closed[-max(120, FEATURE_CONTIGUOUS_CANDLES):]
    timestamps = [int(row[0]) for row in closed]
    if any(
        right - left != timeframe_ms
        for left, right in zip(timestamps[-FEATURE_CONTIGUOUS_CANDLES:], timestamps[-FEATURE_CONTIGUOUS_CANDLES + 1:])
    ):
        raise ValueError("MISSING_PRE_ENTRY_CANDLE")
    result = analyze(closed, volume_ratio_threshold=0.0)
    ema_alignment = (
        1.0 if result["ema20"] > result["ema50"]
        else -1.0 if result["ema20"] < result["ema50"]
        else 0.0
    )
    price_alignment = (
        1.0 if result["price"] > result["ema20"]
        else -1.0 if result["price"] < result["ema20"]
        else 0.0
    )
    return {
        "trend_composite": (ema_alignment + price_alignment) / 2.0,
        "rsi14": float(result["rsi14"]),
        "adx14": float(result["adx14"]),
        "volume_ratio": float(result["volume_ratio"]),
        "atr_pct": float(result["atr_pct"]),
    }


def position_to_sample(
    position: dict[str, Any],
    *,
    wallet_address: str,
    candles: list[list[float]],
    timeframe: str,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        features = features_before_entry(
            candles, _position_signal_timestamp(position), timeframe
        )
    except (ValueError, KeyError) as exc:
        return None, str(exc) or "FEATURE_GENERATION_FAILED"

    raw_reason = str(position.get("exit_reason") or "UNKNOWN")
    position_side = str(position.get("side") or "").strip().upper()
    if position_side not in {"LONG", "SHORT"}:
        return None, "INVALID_POSITION_SIDE"
    direction = 1.0 if position_side == "LONG" else -1.0
    features["trend_composite"] = float(features["trend_composite"]) * direction
    features["rsi14"] = 50.0 + (float(features["rsi14"]) - 50.0) * direction
    lifecycle_issue = str(position.get("invalid_reason") or "")
    reason = lifecycle_issue or raw_reason
    sample_id = hashlib.sha256(
        (
            wallet_address.lower()
            + "|"
            + str(position["coin"])
            + "|"
            + str(_position_signal_timestamp(position))
            + "|"
            + str(position["exit_timestamp"])
        ).encode("utf-8")
    ).hexdigest()[:24]

    sample = {
        "sample_id": sample_id,
        "signal_timestamp": _iso_utc(_position_signal_timestamp(position)),
        "exit_timestamp": _iso_utc(position["exit_timestamp"]),
        "exit_reason": reason,
        "position_side": position_side,
        "features": features,
    }
    if lifecycle_issue:
        # The entry/close timestamps are reconstructable, but this position's
        # lifecycle or accounting is not reliable enough for an R label.
        sample["exclusion_reason"] = lifecycle_issue
        return sample, lifecycle_issue
    if reason == "LIQUIDATION":
        sample["exclusion_reason"] = "LIQUIDATION"
        return sample, "LIQUIDATION"
    if reason in ELIGIBLE_EXIT_REASONS:
        try:
            risk_distance = max(
                2.0 * float(position["atr14"]),
                float(position["entry_price"]) * 0.005,
            )
            initial_risk = risk_distance * float(position["entry_quantity"])
            net_pnl = (
                float(position["gross_pnl_reconstructed"])
                - float(position["fees"])
                + float(position["funding_paid"])
            )
            r_multiple = net_pnl / initial_risk
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
            sample["exit_reason"] = "UNKNOWN_RISK"
            sample["exclusion_reason"] = "INVALID_STANDARDIZED_RISK"
            return sample, "INVALID_STANDARDIZED_RISK"
        if not math.isfinite(initial_risk) or initial_risk <= 0 or not math.isfinite(r_multiple):
            sample["exit_reason"] = "UNKNOWN_RISK"
            sample["exclusion_reason"] = "INVALID_STANDARDIZED_RISK"
            return sample, "INVALID_STANDARDIZED_RISK"
        sample["r_multiple"] = r_multiple
    else:
        sample["exclusion_reason"] = reason
    return sample, None


def _position_atr(position: dict[str, Any], candles: list[list[float]], timeframe: str) -> float:
    timeframe_ms = HOUR_MS if timeframe == "1h" else FOUR_HOURS_MS
    closed = [
        row for row in candles
        if int(row[0]) + timeframe_ms <= _position_signal_timestamp(position)
    ]
    if len(closed) < MIN_FEATURE_CANDLES:
        raise ValueError("INSUFFICIENT_PRE_ENTRY_CANDLES")
    closed = closed[-max(120, FEATURE_CONTIGUOUS_CANDLES):]
    result = analyze(closed, volume_ratio_threshold=0.0)
    return float(result["atr14"])


def prepare_dataset(
    raw_payload: dict[str, Any],
    *,
    code_sha: str,
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES,
) -> dict[str, Any]:
    raw_manifest = raw_payload.get("manifest") or {}
    for field in (
        "source_snapshot_sha256",
        "source_metadata_sha256",
        "cohort_selection_as_of_utc",
        "data_start_utc",
        "data_end_utc",
    ):
        if not raw_manifest.get(field):
            raise ValueError(f"raw dataset is missing {field}")
    if not code_sha:
        raise ValueError("--code-sha is required for reproducibility")
    if not timeframes or len(set(timeframes)) != len(timeframes):
        raise ValueError("timeframes must be non-empty and unique")
    unsupported = sorted(set(timeframes) - {"1h", "4h"})
    if unsupported:
        raise ValueError("unsupported timeframe(s): " + ", ".join(unsupported))

    data_start_ms = int(datetime.fromisoformat(
        raw_manifest["data_start_utc"].replace("Z", "+00:00")
    ).timestamp() * 1000)
    data_end_ms = int(datetime.fromisoformat(
        raw_manifest["data_end_utc"].replace("Z", "+00:00")
    ).timestamp() * 1000)
    if data_end_ms <= data_start_ms:
        raise ValueError("invalid observation window")

    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    reconstruction_exclusions = Counter()
    feature_exclusions = Counter()
    wallet_count = 0
    position_count = 0
    positions_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)

    exchange = ccxt.hyperliquid({
        "enableRateLimit": True,
        "timeout": 60000,
        "options": {"defaultType": "swap"},
    })
    try:
        exchange.load_markets()

        # Collect all positions before requesting candles: leaderboard rank
        # order is unrelated to position entry chronology.
        for wallet in raw_payload.get("wallets") or []:
            wallet_count += 1
            if not raw_wallet_history_complete(wallet):
                reconstruction_exclusions["INCOMPLETE_RAW_WALLET"] += 1
                continue
            positions, exclusions = reconstruct_closed_positions(wallet)
            for reason, count in exclusions.items():
                reconstruction_exclusions[reason] += count
            for position in positions:
                position_count += 1
                if (
                    int(position["entry_timestamp"]) < data_start_ms
                    or int(position["exit_timestamp"]) > data_end_ms
                ):
                    reconstruction_exclusions["POSITION_OUTSIDE_OBSERVATION_WINDOW"] += 1
                    continue
                coin = str(position["coin"])
                try:
                    symbol = _market_symbol(exchange, coin)
                    market = exchange.market(symbol)
                    contract_size = float(market.get("contractSize") or 0.0)
                    if not math.isfinite(contract_size) or not math.isclose(contract_size, 1.0):
                        raise ValueError("unsupported non-unit Hyperliquid perp contract size")
                except (ValueError, TypeError, OverflowError):
                    reconstruction_exclusions["UNSUPPORTED_MARKET_OR_CONTRACT_SIZE"] += 1
                    continue
                positions_by_symbol[symbol].append({
                    **position,
                    "wallet_address": wallet["address"],
                    "symbol": symbol,
                })

        candles_cache: dict[str, list[list[float]]] = {}
        for symbol, symbol_positions in sorted(positions_by_symbol.items()):
            earliest_entry = min(int(row["entry_timestamp"]) for row in symbol_positions)
            candles_cache[symbol] = fetch_hourly_candles(
                exchange,
                symbol,
                max(0, earliest_entry - 300 * HOUR_MS),
                data_end_ms + HOUR_MS,
            )

        for symbol, symbol_positions in sorted(positions_by_symbol.items()):
            hourly = candles_cache[symbol]
            for position in sorted(
                symbol_positions,
                key=lambda row: (
                    int(row["entry_timestamp"]),
                    int(row["exit_timestamp"]),
                    row["wallet_address"],
                ),
            ):
                for timeframe in timeframes:
                    tf_candles = hourly if timeframe == "1h" else aggregate_four_hour(hourly)
                    try:
                        atr_features = _position_atr(position, tf_candles, timeframe)
                    except (ValueError, IndexError):
                        feature_exclusions[
                            f"{symbol}:{timeframe}:MISSING_PRE_ENTRY_FEATURES"
                        ] += 1
                        continue

                    candidate = dict(position)
                    candidate["atr14"] = atr_features
                    sample, problem = position_to_sample(
                        candidate,
                        wallet_address=position["wallet_address"],
                        candles=tf_candles,
                        timeframe=timeframe,
                    )
                    if sample is None:
                        feature_exclusions[
                            f"{symbol}:{timeframe}:{problem or 'UNKNOWN'}"
                        ] += 1
                        continue

                    group_key = ("hyperliquid", symbol, "futures", timeframe)
                    group = groups.setdefault(group_key, {
                        "exchange": "hyperliquid",
                        "symbol": symbol,
                        "market_type": "futures",
                        "timeframe": timeframe,
                        "samples": [],
                        "provenance": {},
                        "excluded_samples": 0,
                    })
                    sample.update({
                        "exchange": "hyperliquid",
                        "symbol": symbol,
                        "market_type": "futures",
                        "timeframe": timeframe,
                    })
                    group["samples"].append(sample)
                    if sample["exit_reason"] not in ELIGIBLE_EXIT_REASONS:
                        group["excluded_samples"] += 1
                        feature_exclusions[
                            f"{symbol}:{timeframe}:{sample['exit_reason']}"
                        ] += 1

        manifest = {
            "source_provider": raw_manifest.get("source_provider"),
            "source_url": raw_manifest.get("source_url"),
            "source_metadata_url": raw_manifest.get("source_metadata_url"),
            "source_license": raw_manifest.get("source_license"),
            "source_snapshot_utc": raw_manifest.get("source_snapshot_utc"),
            "leaderboard_retrieved_at_utc": raw_manifest.get("leaderboard_retrieved_at_utc"),
            "source_snapshot_sha256": raw_manifest.get("source_snapshot_sha256"),
            "source_metadata_sha256": raw_manifest.get("source_metadata_sha256"),
            "raw_dataset_sha256": raw_manifest.get("raw_dataset_sha256"),
            "cohort_selection_as_of_utc": raw_manifest.get("cohort_selection_as_of_utc"),
            "data_start_utc": raw_manifest.get("data_start_utc"),
            "data_end_utc": raw_manifest.get("data_end_utc"),
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "code_sha": code_sha,
            "preparation_code_sha": code_sha,
            "backtest_config": {
                "risk_unit_definition": (
                    "system-normalized R = (reconstructed gross fill-price PnL - "
                    "all signed USDC/USD fees + funding delta.usdc) / "
                    "(max(2*ATR14, 0.5% of entry price) * original coin quantity); "
                    "not the trader's unknown original stop risk"
                ),
                "fee_model": (
                    "sum signed fee from every open and close fill; positive fee is a cost "
                    "and negative fee is a maker rebate; builderFee is included in fee"
                ),
                "closed_pnl_treatment": (
                    "retain exchange closedPnl for audit only; reconstruct gross price PnL "
                    "from entry price, closing fill prices, direction and closed quantities"
                ),
                "slippage_model": "actual public fill prices; no synthetic slippage added",
                "slippage_bps": 0.0,
                "fee_rate": 0.0,
                "funding_included": True,
                "entry_rule": (
                    "first non-scaled Open Long/Open Short fill; partial fills from the same "
                    "opening oid use volume-weighted entry and final fill time; funding starts at first fill"
                ),
                "exit_rule": (
                    "single fully closing reduceOnly trigger order; Stop/Take type and "
                    "direction-compatible trigger condition determine label; manual/ambiguous exits excluded"
                ),
                "feature_directionality": (
                    "trend_composite and RSI14 transformed to position-relative LONG/SHORT "
                    "orientation; ADX14, Volume Ratio and ATR% are direction-neutral"
                ),
            },
        }
        prepared_groups = []
        for key in sorted(groups):
            group = groups[key]
            group["samples"].sort(
                key=lambda row: (
                    row["signal_timestamp"],
                    row["exit_timestamp"],
                    row["sample_id"],
                )
            )
            prepared_groups.append(group)

        return {
            "dataset_version": 2,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_raw_dataset_version": raw_payload.get("dataset_version"),
            "manifest": manifest,
            "preparation_policy": {
                "position_lifecycle": (
                    "single entry order, no add/scale-in, fully closed only; reconcile startPosition; "
                    "signal/features are anchored to first fill of the opening oid; funding also starts there"
                ),
                "contract_multiplier": "only linear Hyperliquid perps with CCXT contractSize exactly 1.0 are included",
                "eligible_exit_reasons": sorted(ELIGIBLE_EXIT_REASONS),
                "ineligible_exit_reasons": [
                    "MANUAL_CLOSE", "UNKNOWN", "LIQUIDATION", "UNKNOWN_RISK", "PERIOD_END",
                ],
                "trigger_classification": (
                    "STOP/TARGET from orderType plus direction-compatible triggerCondition; "
                    "no entry-price test, so moved/trailing stops do not become targets"
                ),
                "feature_time": "last fully closed OHLCV candle before first entry fill",
                "feature_directionality": "trend_composite and RSI14 are position-relative",
                "hourly_source": "Hyperliquid public CCXT OHLCV 1h",
                "four_hour_source": "aggregate only four consecutive UTC 1h candles; no fill/interpolation; require 120 contiguous feature bars",
                "funding": "sum valid same-coin delta.usdc rows from first opening fill to final close; malformed rows invalidate label",
                "ohlcv_coverage": (
                    "fetch from earliest first fill per symbol with 120 contiguous 4h candles of warmup, "
                    "independent of wallet ranking"
                ),
            },
            "coverage": {
                "wallets_seen": wallet_count,
                "positions_reconstructed": position_count,
                "symbols_with_candle_history": len(candles_cache),
                "groups_created": len(prepared_groups),
                "samples_created": sum(len(group["samples"]) for group in prepared_groups),
                "reconstruction_exclusions": dict(sorted(reconstruction_exclusions.items())),
                "feature_and_label_exclusions": dict(sorted(feature_exclusions.items())),
            },
            "groups": prepared_groups,
        }
    finally:
        exchange.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dataset", required=True)
    parser.add_argument("--out", default="artifacts/advisor-training-dataset.json")
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--timeframes", default="1h,4h")
    args = parser.parse_args()

    raw_path = Path(args.raw_dataset)
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    timeframes = tuple(
        item.strip() for item in args.timeframes.split(",") if item.strip()
    )
    prepared = prepare_dataset(
        raw_payload,
        code_sha=args.code_sha,
        timeframes=timeframes,
    )
    prepared["manifest"]["raw_dataset_sha256"] = _sha256_file(raw_path)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        prepared,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    output.write_bytes(encoded)
    print(f"OUTPUT {output}")
    print(f"DATASET_SHA256 {hashlib.sha256(encoded).hexdigest()}")
    print(json.dumps(prepared["coverage"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
