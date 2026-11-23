#!/usr/bin/env python3
"""Vike V3.1 fail-closed Triple-Barrier research pipeline.

Reads only the frozen 30-month Binance USD-M development archive under
raw-staging. It writes research artifacts under derived/research-pipeline.
No network calls, raw-file mutation, holdout access, or trading actions occur.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.binance_usdm_liquidation import (
    MaintenanceBracket,
    calculate_isolated_liquidation_price,
)
from backend.causal_market_features import validate_mark_price_alignment
from backend.triple_barrier_labels import Bar, FundingEvent, LabelConfig, label_event
from scripts.vike_exploratory_train import (
    BAR_MS,
    FEATURES,
    HOUR_MS,
    MONTHS,
    SYMBOLS,
    aggregate_context,
    complete_day_bars,
    context_returns,
    month_bounds,
    read_trade_or_mark,
    resolve_csv,
    rsi14,
    sha256_file,
    split_contiguous,
    wilder_atr_values,
)

MAX_HOLDING_BARS = 48
EMBARGO_MS = MAX_HOLDING_BARS * BAR_MS
FOLDS = (
    ("2024-11", "2025-01", "2025-02", "2025-04"),
    ("2025-02", "2025-04", "2025-05", "2025-07"),
    ("2025-05", "2025-07", "2025-08", "2025-10"),
    ("2025-08", "2025-10", "2025-11", "2026-01"),
    ("2025-11", "2026-01", "2026-02", "2026-04"),
)


def _month_start(month: str) -> int:
    return month_bounds(month)[0]


def _month_end(month: str) -> int:
    return month_bounds(month)[1]


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def load_funding_attestation(path: Path | None, symbols: list[str]) -> tuple[dict, str]:
    if path is None or not path.is_file():
        return {}, "NOT_VERIFIED"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("coverage_status") != "VERIFIED":
        return {}, "NOT_VERIFIED"
    if not _valid_sha256(payload.get("evidence_sha256")):
        return {}, "NOT_VERIFIED"
    if not payload.get("reviewed_by") or not payload.get("reviewed_at_utc") or not payload.get("semantic_basis"):
        return {}, "NOT_VERIFIED"
    entries = payload.get("symbol_months")
    if not isinstance(entries, list):
        return {}, "NOT_VERIFIED"
    index = {}
    for item in entries:
        if not isinstance(item, dict):
            return {}, "NOT_VERIFIED"
        key = (str(item.get("symbol", "")).upper(), str(item.get("month", "")))
        if key in index or item.get("status") != "VERIFIED" or not _valid_sha256(item.get("funding_file_sha256")):
            return {}, "NOT_VERIFIED"
        index[key] = item
    expected = {(symbol, month) for symbol in symbols for month in MONTHS}
    if set(index) != expected:
        return {}, "NOT_VERIFIED"
    return index, "VERIFIED"


def load_liquidation_manifest(path: Path | None, symbols: list[str]) -> tuple[list[dict], str]:
    if path is None or not path.is_file():
        return [], "NOT_VERIFIED"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("status") != "VERIFIED":
        return [], "NOT_VERIFIED"
    if not _valid_sha256(payload.get("evidence_sha256")):
        return [], "NOT_VERIFIED"
    if not payload.get("reviewed_by") or not payload.get("reviewed_at_utc"):
        return [], "NOT_VERIFIED"
    positions = payload.get("positions")
    if not isinstance(positions, list) or not positions:
        return [], "NOT_VERIFIED"
    validated = []
    for row in positions:
        if not isinstance(row, dict):
            return [], "NOT_VERIFIED"
        required = ("symbol", "valid_from_ms", "valid_to_ms", "quantity",
                    "isolated_wallet_balance", "brackets")
        if any(key not in row for key in required):
            return [], "NOT_VERIFIED"
        if row.get("margin_mode") != "ISOLATED" or row.get("position_mode") != "ONE_WAY":
            continue
        if row.get("contract_type") != "USDT_LINEAR" or row.get("no_external_flows") is not True:
            continue
        if not isinstance(row.get("valid_from_ms"), int) or not isinstance(row.get("valid_to_ms"), int):
            continue
        if row["valid_to_ms"] <= row["valid_from_ms"]:
            continue
        if not _valid_sha256(row.get("snapshot_sha256")):
            continue
        try:
            quantity = float(row["quantity"])
            wallet = float(row["isolated_wallet_balance"])
            brackets = [MaintenanceBracket(
                float(x["notional_floor"]), float(x["notional_cap"]),
                float(x["maint_margin_ratio"]), float(x["cum"]),
            ) for x in row["brackets"]]
            if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(wallet) or wallet <= 0:
                continue
            # The calculator validates contiguity and tier values.
            from backend.binance_usdm_liquidation import validate_brackets
            validate_brackets(brackets)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        validated.append({**row, "quantity": quantity, "isolated_wallet_balance": wallet,
                          "_brackets": brackets})
    if not validated:
        return [], "NOT_VERIFIED"
    # The manifest must cover the entire development window for every symbol;
    # event-level matching below still rejects overlaps or missing snapshots.
    window_start, window_end = _month_start(MONTHS[0]), _month_end(MONTHS[-1])
    for symbol in symbols:
        intervals = sorted(
            (row["valid_from_ms"], row["valid_to_ms"])
            for row in validated if str(row["symbol"]).upper() == symbol
        )
        cursor = window_start
        for start_ms, end_ms in intervals:
            if start_ms > cursor:
                break
            cursor = max(cursor, end_ms)
        if cursor < window_end:
            return [], "NOT_VERIFIED"
    return validated, "VERIFIED"


def _read_funding(path: Path, start: int, end: int) -> list[FundingEvent]:
    rows = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"funding_time", "funding_rate", "mark_price"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path}: Funding schema missing required columns")
        for line, row in enumerate(reader, 2):
            timestamp = int(row["funding_time"])
            rate, mark = float(row["funding_rate"]), float(row["mark_price"])
            if not start <= timestamp < end or not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise ValueError(f"{path}:{line}: invalid Funding row")
            rows.append(FundingEvent(timestamp, rate, mark))
    if not rows:
        raise ValueError(f"{path}: Funding file contains no valid records")
    rows.sort(key=lambda item: item.funding_time_ms)
    if len({item.funding_time_ms for item in rows}) != len(rows):
        raise ValueError(f"{path}: duplicate Funding timestamps")
    return rows


def build_opportunity_events(symbol: str, bars: list[dict]) -> list[dict]:
    """Frozen Donchian-20 generator; no outcome, future-bar or label filtering."""
    if len(bars) < 21:
        return []
    atrs = wilder_atr_values(bars)
    h1c, h1v = aggregate_context(bars, HOUR_MS, 4)
    h4c, h4v = aggregate_context(bars, 4 * HOUR_MS, 16)
    events = []
    armed = {"LONG": True, "SHORT": True}
    for i in range(20, len(bars)):
        bar = bars[i]
        prior = bars[i - 20:i]
        upper = max(item["h"] for item in prior)
        lower = min(item["l"] for item in prior)
        if lower <= bar["c"] <= upper:
            armed = {"LONG": True, "SHORT": True}
        side = "LONG" if bar["c"] > upper else "SHORT" if bar["c"] < lower else None
        if side is None or not armed[side] or atrs[i] is None or atrs[i] <= 0:
            continue
        width = upper - lower
        mean_volume = statistics.fmean(item["v"] for item in prior)
        signal_close = bar["close_t"]
        h1r1, h1r4 = context_returns(h1c, h1v, signal_close)
        h4r1, h4r4 = context_returns(h4c, h4v, signal_close)
        hour = (bar["t"] // HOUR_MS) % 24
        features = {
            "ret_1": bar["c"] / bars[i - 1]["c"] - 1.0,
            "ret_4": bar["c"] / bars[i - 4]["c"] - 1.0,
            "ret_16": bar["c"] / bars[i - 16]["c"] - 1.0,
            "atr_pct": atrs[i] / bar["c"],
            "donchian_width_pct": width / bar["c"] if width else 0.0,
            "channel_position": (bar["c"] - lower) / width if width else 0.5,
            "volume_ratio": bar["v"] / mean_volume if mean_volume > 0 else 0.0,
            "rsi14": rsi14(bars, i),
            "hour_sin": math.sin(2 * math.pi * hour / 24.0),
            "hour_cos": math.cos(2 * math.pi * hour / 24.0),
            "htf_1h_ret_1": h1r1, "htf_1h_ret_4": h1r4,
            "htf_4h_ret_1": h4r1, "htf_4h_ret_4": h4r4,
        }
        if not all(math.isfinite(value) for value in features.values()):
            armed[side] = False
            continue
        entry_time = bars[i + 1]["t"] if i + 1 < len(bars) else None
        events.append({
            "symbol": symbol, "signal_time_ms": signal_close,
            "entry_time_ms": entry_time, "side": side, "atr": float(atrs[i]),
            "features": features, "event_status": "GENERATED",
        })
        armed[side] = False
    return events


def apply_overlap_control(events: list[dict], max_holding_bars: int = MAX_HOLDING_BARS) -> list[dict]:
    """Outcome-independent per-symbol cooldown; preserves rejected candidates."""
    cooldown = max_holding_bars * BAR_MS
    last_kept = {}
    result = []
    for event in sorted(events, key=lambda row: (row["symbol"], row["signal_time_ms"])):
        prior = last_kept.get(event["symbol"])
        if prior is not None and event["signal_time_ms"] < prior + cooldown:
            event["overlap_status"] = "EXCLUDED_FIXED_HORIZON_COOLDOWN"
        else:
            event["overlap_status"] = "ELIGIBLE_NON_OVERLAPPING"
            last_kept[event["symbol"]] = event["signal_time_ms"]
        result.append(event)
    return result


def _to_bar(row: dict, prefix: str = "") -> Bar:
    return Bar(row["t"], row["close_t"], row[f"{prefix}o"], row[f"{prefix}h"],
               row[f"{prefix}l"], row[f"{prefix}c"])


def _liquidation_position(positions: list[dict], symbol: str, entry_time: int) -> dict | None:
    matches = [row for row in positions if str(row["symbol"]).upper() == symbol
               and row["valid_from_ms"] <= entry_time < row["valid_to_ms"]]
    return matches[0] if len(matches) == 1 else None


def label_opportunity(event: dict, segment: list[dict], mark_by_time: dict[int, dict],
                      funding: list[FundingEvent], funding_verified: bool,
                      positions: list[dict], liquidation_verified: bool,
                      fee_bps: float, slippage_bps: float) -> dict:
    if event["overlap_status"] != "ELIGIBLE_NON_OVERLAPPING":
        return {"status": "excluded", "reason": event["overlap_status"]}
    if not funding_verified:
        return {"status": "excluded", "reason": "funding_coverage_not_verified"}
    if not liquidation_verified:
        return {"status": "excluded", "reason": "historical_liquidation_brackets_or_account_state_not_verified"}
    if event["entry_time_ms"] is None:
        return {"status": "excluded", "reason": "no_next_bar"}
    trade_rows = [row for row in segment if row["t"] >= event["entry_time_ms"]][:MAX_HOLDING_BARS]
    if not trade_rows:
        return {"status": "excluded", "reason": "no_next_bar"}
    if len(trade_rows) < MAX_HOLDING_BARS:
        return {"status": "excluded", "reason": "insufficient_matured_horizon"}
    mark_rows = [mark_by_time.get(row["t"]) for row in trade_rows]
    if any(row is None for row in mark_rows):
        return {"status": "excluded", "reason": "mark_price_coverage_not_verified"}
    try:
        validate_mark_price_alignment(
            [_to_bar(row) for row in trade_rows],
            [_to_bar(row, "mark_") for row in mark_rows],
        )
    except ValueError as exc:
        return {"status": "excluded", "reason": "mark_price_alignment_failed", "detail": str(exc)}
    # Funding changes isolated wallet balance and therefore liquidation price.
    # We first resolve the actual barrier exit, then reject only if a settlement
    # occurred before that exit. Settlements after an early exit do not affect
    # the position and need not exclude the event.
    entry_time = event["entry_time_ms"]
    position = _liquidation_position(positions, event["symbol"], entry_time)
    if position is None:
        return {"status": "excluded", "reason": "no_time_matched_historical_liquidation_snapshot"}
    direction = 1.0 if event["side"] == "LONG" else -1.0
    slip = slippage_bps / 10_000.0
    entry_fill = trade_rows[0]["o"] * (1.0 + direction * slip)
    try:
        liquidation = calculate_isolated_liquidation_price(
            side=event["side"], entry_price=entry_fill,
            quantity=position["quantity"],
            isolated_wallet_balance=position["isolated_wallet_balance"],
            brackets=position["_brackets"],
        )
    except ValueError as exc:
        return {"status": "excluded", "reason": "liquidation_calculation_failed", "detail": str(exc)}
    try:
        result = label_event(
            event_time_ms=event["signal_time_ms"], side=event["side"], atr=event["atr"],
            bars=[_to_bar(row) for row in trade_rows],
            funding_events=funding, funding_coverage_verified=True,
            mark_price_bars=[_to_bar(row, "mark_") for row in mark_rows],
            liquidation_price=liquidation["liquidation_price"],
            mark_price_coverage_verified=True, liquidation_verification=liquidation,
            config=LabelConfig(
                atr_multiple_stop=1.5, atr_multiple_target=3.0,
                max_holding_bars=MAX_HOLDING_BARS,
                taker_fee_rate=fee_bps / 10_000.0,
                adverse_slippage_rate=slippage_bps / 10_000.0,
            ),
        )
    except ValueError as exc:
        return {"status": "excluded", "reason": "label_precondition_failed", "detail": str(exc)}
    if result.get("status") == "labeled":
        exit_time = result["exit_time_ms"]
        funding_during_position = [
            item for item in funding
            if entry_time <= item.funding_time_ms <= exit_time
        ]
        if funding_during_position:
            return {
                "status": "excluded",
                "reason": "dynamic_liquidation_margin_after_funding_not_modeled",
                "funding_events_during_position": len(funding_during_position),
            }
    return result


def _fold_bounds(row: tuple[str, str, str, str]) -> dict:
    val_start, val_end, test_start, test_end = row
    return {
        "validation_start_ms": _month_start(val_start),
        "validation_end_ms": _month_end(val_end),
        "test_start_ms": _month_start(test_start),
        "test_end_ms": _month_end(test_end),
        "validation_months": [val_start, val_end],
        "test_months": [test_start, test_end],
    }


def run_walk_forward(labels: list[dict], symbols: list[str] | None = None,
                      output_dir: Path | None = None,
                      min_examples_per_symbol: int = 200) -> dict:
    selected_symbols = symbols or list(SYMBOLS)
    counts = Counter(row["symbol"] for row in labels)
    if not labels:
        return {"status": "BLOCKED_NO_VERIFIED_LABELS", "folds": [],
                "per_symbol_label_counts": dict(counts), "lightgbm": "NOT_RUN"}
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import precision_score, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    results = []
    for index, fold in enumerate(FOLDS, 1):
        bounds = _fold_bounds(fold)
        val_start, val_end = bounds["validation_start_ms"], bounds["validation_end_ms"]
        test_start, test_end = bounds["test_start_ms"], bounds["test_end_ms"]
        train = [row for row in labels if row["signal_time_ms"] < val_start - EMBARGO_MS
                 and row["exit_time_ms"] < val_start - EMBARGO_MS]
        validation = [row for row in labels if val_start <= row["signal_time_ms"] < val_end
                      and row["exit_time_ms"] < test_start - EMBARGO_MS]
        test = [row for row in labels if test_start <= row["signal_time_ms"] < test_end - EMBARGO_MS
                and row["exit_time_ms"] < test_end]
        per_symbol_train = {symbol: sum(row["symbol"] == symbol for row in train) for symbol in selected_symbols}
        if (len(train) < 3000 or len(validation) < 100 or len(test) < 50
                or any(per_symbol_train[symbol] < min_examples_per_symbol for symbol in selected_symbols)
                or len({row["label"] for row in train}) < 2):
            results.append({"fold": index, **bounds, "status": "INCONCLUSIVE_INSUFFICIENT_SAMPLES",
                            "train_count": len(train), "validation_count": len(validation),
                            "test_count": len(test), "per_symbol_train": per_symbol_train})
            continue
        x_train = np.asarray([[row["features"][key] for key in FEATURES] for row in train], dtype=float)
        y_train = np.asarray([row["label"] for row in train], dtype=int)
        x_val = np.asarray([[row["features"][key] for key in FEATURES] for row in validation], dtype=float)
        x_test = np.asarray([[row["features"][key] for key in FEATURES] for row in test], dtype=float)
        model = Pipeline([
            ("scale", StandardScaler()),
            ("logistic", LogisticRegression(C=0.1, max_iter=3000, class_weight="balanced", random_state=31)),
        ])
        model.fit(x_train, y_train)
        val_prob = model.predict_proba(x_val)[:, 1]
        threshold = float(np.quantile(val_prob, 2.0 / 3.0))
        test_prob = model.predict_proba(x_test)[:, 1]
        selected = [row for row, probability in zip(test, test_prob) if probability >= threshold]
        model_artifact = None
        model_sha256 = None
        if output_dir is not None:
            import joblib
            model_path = output_dir / f"fold_{index:02d}_logistic_regression.joblib"
            joblib.dump(model, model_path)
            model_artifact = model_path.name
            model_sha256 = sha256_file(model_path)
        per_symbol = {}
        for symbol in selected_symbols:
            rows = [row for row in test if row["symbol"] == symbol]
            chosen = [row for row, probability in zip(test, test_prob)
                      if row["symbol"] == symbol and probability >= threshold]
            per_symbol[symbol] = {
                "test_count": len(rows), "selected_count": len(chosen),
                "mean_net_r_all": statistics.fmean(row["net_r"] for row in rows) if rows else None,
                "mean_net_r_selected": statistics.fmean(row["net_r"] for row in chosen) if chosen else None,
            }
        results.append({
            "fold": index, **bounds, "status": "SCORED",
            "train_count": len(train), "validation_count": len(validation), "test_count": len(test),
            "selected_count": len(selected), "validation_top_tercile_threshold": threshold,
            "model_artifact": model_artifact, "model_sha256": model_sha256,
            "mean_net_r_all": statistics.fmean(row["net_r"] for row in test),
            "mean_net_r_selected": statistics.fmean(row["net_r"] for row in selected) if selected else None,
            "precision_selected": precision_score(
                [row["label"] for row in test],
                [int(probability >= threshold) for probability in test_prob],
                zero_division=0,
            ),
            "roc_auc": float(roc_auc_score([row["label"] for row in test], test_prob))
                       if len({row["label"] for row in test}) == 2 else None,
            "per_symbol": per_symbol,
        })
    scored = [row for row in results if row["status"] == "SCORED"]
    return {
        "status": "DIAGNOSTIC_ONLY_BOOTSTRAP_AND_FINAL_GATES_PENDING" if len(scored) >= 3
                  else "INCONCLUSIVE_FEWER_THAN_THREE_VALID_FOLDS",
        "folds": results, "valid_fold_count": len(scored),
        "per_symbol_label_counts": dict(counts),
        "per_symbol_minimum_200_pass": {symbol: counts[symbol] >= min_examples_per_symbol for symbol in selected_symbols},
        "pooled_minimum_3000_pass": len(labels) >= 3000,
        "lightgbm": "NOT_RUN_LOGISTIC_REGRESSION_BASELINE_MUST_BE_VALID_FIRST",
        "acceptance": "NOT_APPROVED_BLOCK_BOOTSTRAP_AND_ALL_PREREGISTERED_GATES_REQUIRED",
    }


def run(args) -> dict:
    drive_root = Path(args.drive_root).expanduser().resolve()
    raw_root = (drive_root / "raw-staging").resolve()
    output_root = (drive_root / "derived" / "research-pipeline").resolve()
    if not raw_root.is_dir():
        raise FileNotFoundError(f"raw-staging directory not found: {raw_root}")
    if raw_root == output_root or raw_root in output_root.parents:
        raise ValueError("output must remain outside raw-staging")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols or len(set(symbols)) != len(symbols) or any(item not in SYMBOLS for item in symbols):
        raise ValueError("symbols must be a unique subset of the frozen eight-symbol universe")
    if args.fee_bps_per_side < 0 or args.slippage_bps_per_side < 0:
        raise ValueError("fee and slippage assumptions must be nonnegative")

    funding_attestation, funding_status = load_funding_attestation(args.funding_attestation, symbols)
    liquidation_positions, liquidation_status = load_liquidation_manifest(args.liquidation_manifest, symbols)
    monthly_audits, exclusions, hashes, all_events, labeled = [], [], [], [], []
    funding_hashes_match = True
    symbol_segments = {}
    funding_by_symbol = {}
    funding_verified_by_symbol = {}

    for symbol in symbols:
        symbol_trade, symbol_mark, symbol_funding = {}, {}, []
        symbol_funding_verified = True
        for month in MONTHS:
            start, end = month_bounds(month)
            files = {stream: resolve_csv(raw_root, month, symbol, stream)
                     for stream in ("trade_15m", "mark_15m", "funding")}
            for stream, path in files.items():
                hashes.append({"symbol": symbol, "month": month, "stream": stream,
                               "path_relative": str(path.relative_to(drive_root)),
                               "sha256": sha256_file(path), "bytes": path.stat().st_size})
            trade = read_trade_or_mark(files["trade_15m"], "trade_15m", start, end)
            mark = read_trade_or_mark(files["mark_15m"], "mark_15m", start, end)
            funding = _read_funding(files["funding"], start, end)
            if not funding:
                raise ValueError(f"{symbol}/{month}: Funding CSV has no valid records")
            eligible, excluded = complete_day_bars(trade, mark, start, end)
            funding_item = funding_attestation.get((symbol, month))
            source_funding_hash = next(item["sha256"] for item in hashes
                                       if item["symbol"] == symbol and item["month"] == month
                                       and item["stream"] == "funding")
            month_funding_verified = bool(
                funding_status == "VERIFIED" and funding_item
                and funding_item.get("funding_file_sha256") == source_funding_hash
            )
            funding_hashes_match = funding_hashes_match and month_funding_verified
            symbol_funding_verified = symbol_funding_verified and month_funding_verified
            monthly_audits.append({
                "symbol": symbol, "month": month, "trade_rows": len(trade), "mark_rows": len(mark),
                "funding_rows": len(funding), "price_eligible_days": len(eligible) // 96,
                "excluded_days": excluded, "funding_structural_status": "PASS" if funding else "FAIL",
                "funding_coverage_status": "VERIFIED" if month_funding_verified else "NOT_VERIFIED",
            })
            exclusions.extend({"symbol": symbol, "month": month, **item} for item in excluded)
            for row in eligible:
                symbol_trade[row["t"]] = row
                symbol_mark[row["t"]] = {
                    "t": row["t"], "close_t": mark[row["t"]]["close_t"],
                    "mark_o": mark[row["t"]]["o"], "mark_h": mark[row["t"]]["h"],
                    "mark_l": mark[row["t"]]["l"], "mark_c": mark[row["t"]]["c"],
                }
            symbol_funding.extend(funding)
        ordered = [symbol_trade[t] for t in sorted(symbol_trade)]
        segments = split_contiguous(ordered)
        symbol_segments[symbol] = [(segment, symbol_mark) for segment in segments]
        funding_by_symbol[symbol] = sorted(symbol_funding, key=lambda item: item.funding_time_ms)
        funding_verified_by_symbol[symbol] = symbol_funding_verified
        for segment in segments:
            all_events.extend(build_opportunity_events(symbol, segment))

    if funding_status == "VERIFIED" and not funding_hashes_match:
        funding_status = "NOT_VERIFIED"
    all_events = apply_overlap_control(all_events)
    for event in all_events:
        event["label_status"] = "NOT_ATTEMPTED"
        event["label_reason"] = "event_segment_not_resolved"
    for symbol, segments in symbol_segments.items():
        funding = funding_by_symbol[symbol]
        funding_verified = funding_verified_by_symbol[symbol]
        for segment, mark_map in segments:
            segment_signal_times = {row["close_t"] for row in segment}
            candidates = [row for row in all_events
                          if row["symbol"] == symbol and row["signal_time_ms"] in segment_signal_times]
            for event in candidates:
                result = label_opportunity(
                    event, segment, mark_map, funding, funding_verified,
                    liquidation_positions, liquidation_status == "VERIFIED",
                    args.fee_bps_per_side, args.slippage_bps_per_side,
                )
                event["label_status"] = result.get("status", "excluded")
                event["label_reason"] = result.get("reason")
                if result.get("status") == "labeled":
                    event.update({key: result[key] for key in (
                        "label", "net_r", "exit_time_ms", "exit_reason", "entry_fill", "exit_fill",
                        "stop_price", "target_price", "funding_cashflow_per_unit",
                    )})
                    event["label_status"] = "LABELED_TRIPLE_BARRIER"
                    labeled.append(event)
                else:
                    event["label_detail"] = result.get("detail")

    output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=False, exist_ok=False)
    walk_forward = run_walk_forward(labeled, symbols, output_dir)
    per_symbol = {}
    for symbol in symbols:
        rows = [event for event in all_events if event["symbol"] == symbol]
        per_symbol[symbol] = {
            "events": len(rows),
            "long_events": sum(event["side"] == "LONG" for event in rows),
            "short_events": sum(event["side"] == "SHORT" for event in rows),
            "overlap_excluded": sum(event["overlap_status"] != "ELIGIBLE_NON_OVERLAPPING" for event in rows),
            "triple_barrier_labeled": sum(event["label_status"] == "LABELED_TRIPLE_BARRIER" for event in rows),
            "label_exclusion_reasons": dict(Counter(
                event.get("label_reason") or event["label_status"] for event in rows
                if event["label_status"] != "LABELED_TRIPLE_BARRIER"
            )),
        }
    acceptance_blockers = []
    if funding_status != "VERIFIED":
        acceptance_blockers.append(
            "Funding semantics and historical coverage are not verified against all selected symbol-month source hashes."
        )
    if liquidation_status != "VERIFIED":
        acceptance_blockers.append(
            "Historical liquidation brackets and time-matched isolated position assumptions do not cover the full development window."
        )
    if any(event.get("label_reason") == "dynamic_liquidation_margin_after_funding_not_modeled"
           for event in all_events):
        acceptance_blockers.append(
            "Events with Funding settlements before their barrier exit are excluded until dynamic isolated-margin updates are modeled."
        )
    if walk_forward.get("valid_fold_count", 0) < 3:
        acceptance_blockers.append("At least three valid purged/embargoed walk-forward folds are required.")
    acceptance_blockers.extend([
        "Synchronized block bootstrap, calibration uncertainty, doubled-cost tests, and the remaining preregistered acceptance gates are pending.",
        "The locked May-October 2026 holdout remains untouched and requires separate one-time authorization.",
    ])
    report = {
        "schema_version": 1, "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": "RESEARCH_BLOCKED_NOT_MODEL_APPROVAL" if not labeled
                   else "WALK_FORWARD_DIAGNOSTIC_NOT_MODEL_APPROVAL",
        "venue": "Binance USD-M", "market_type": "USDT perpetual futures", "timeframe": "15m",
        "symbols": symbols, "development_months": list(MONTHS),
        "locked_holdout_months_accessed": False,
        "raw_data_mutated": False,
        "fee_bps_per_side": args.fee_bps_per_side,
        "slippage_bps_per_side": args.slippage_bps_per_side,
        "label_policy": "TRIPLE_BARRIER_STOP_1_5_ATR_TARGET_3_ATR_MAX_48_BARS",
        "event_generator": "DONCHIAN_20_DIRECTION_NEUTRAL_CLOSED_15M",
        "overlap_policy": "FIXED_48_BAR_PER_SYMBOL_COOLDOWN_OUTCOME_INDEPENDENT",
        "funding_coverage_status": funding_status,
        "funding_label_gate": "PASS" if funding_status == "VERIFIED" else "BLOCKED_NOT_VERIFIED",
        "liquidation_verification_status": liquidation_status,
        "liquidation_policy": "HISTORICAL_ISOLATED_ONE_WAY_SNAPSHOT_REQUIRED; dynamic funding margin changes are excluded",
        "event_count": len(all_events), "labeled_event_count": len(labeled),
        "per_symbol": per_symbol,
        "walk_forward": walk_forward,
        "lightgbm_status": "NOT_RUN_UNTIL_LOGISTIC_REGRESSION_WFO_AND_PREREGISTERED_COMPARISON_MARGIN_PASS",
        "acceptance_status": "NOT_APPROVED",
        "acceptance_blockers": acceptance_blockers,
        "source_file_hashes": hashes,
    }
    (output_dir / "research_report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "monthly_audits.json").write_text(json.dumps(monthly_audits, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "excluded_days.json").write_text(json.dumps(exclusions, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "walk_forward_results.json").write_text(json.dumps(walk_forward, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with (output_dir / "event_manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["symbol", "signal_time_ms", "entry_time_ms", "side", "atr", "overlap_status",
                  "label_status", "label_reason", "label", "net_r", "exit_time_ms", *FEATURES]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for event in sorted(all_events, key=lambda row: (row["signal_time_ms"], row["symbol"])):
            writer.writerow({**event, **event["features"]})
    return {"output_dir": str(output_dir), "report": report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-root", required=True)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--fee-bps-per-side", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    parser.add_argument("--funding-attestation", type=Path)
    parser.add_argument("--liquidation-manifest", type=Path)
    args = parser.parse_args()
    try:
        result = run(args)
        report = result["report"]
        print(json.dumps({
            "output_dir": result["output_dir"], "verdict": report["verdict"],
            "events": report["event_count"], "labeled": report["labeled_event_count"],
            "funding": report["funding_coverage_status"],
            "liquidation": report["liquidation_verification_status"],
            "walk_forward": report["walk_forward"]["status"],
        }, indent=2))
        return 0
    except Exception as exc:
        parser.exit(2, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
