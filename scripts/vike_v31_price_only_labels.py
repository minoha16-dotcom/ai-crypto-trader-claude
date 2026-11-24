#!/usr/bin/env python3
"""Vike V3.1 Price-only Triple-Barrier label preparation.

This path deliberately does NOT read or infer historical maintenance brackets,
position size, leverage, margin, or liquidation. It uses Mark Price OHLC to
trigger barriers, trade OHLC for next-bar-open entry, explicit fees/slippage,
and excludes any event whose open-position interval overlaps a Funding
settlement. Funding cashflows are not applied to net_r.

Reads only the frozen Binance USD-M development archive (Nov 2023-Apr 2026)
and writes only beneath derived/price-only-labels. It never trains a model,
downloads data, mutates raw-staging, or accesses the locked May-Oct 2026 holdout.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from backend.causal_market_features import validate_mark_price_alignment
from scripts.vike_exploratory_train import (
    BAR_MS,
    FEATURES,
    MONTHS,
    SYMBOLS,
    complete_day_bars,
    month_bounds,
    read_trade_or_mark,
    sha256_file,
    split_contiguous,
)
from scripts.vike_v31_research_pipeline import (
    MAX_HOLDING_BARS,
    _read_funding,
    _to_bar,
    apply_overlap_control,
    build_opportunity_events,
    load_funding_attestation,
)

FEE_BPS_PER_SIDE = 5.0
SLIPPAGE_BPS_PER_SIDE = 2.0
STOP_ATR = 1.5
TARGET_ATR = 3.0


def label_price_only_event(
    event: dict,
    trade_segment: list[dict],
    mark_by_time: dict[int, dict],
    funding_events: list,
    fee_bps_per_side: float = FEE_BPS_PER_SIDE,
    slippage_bps_per_side: float = SLIPPAGE_BPS_PER_SIDE,
) -> dict:
    """Label one event using Mark Price barriers and no liquidation model."""
    if event.get("overlap_status") != "ELIGIBLE_NON_OVERLAPPING":
        return {"status": "excluded", "reason": event.get("overlap_status", "overlap_not_verified")}
    if event.get("entry_time_ms") is None:
        return {"status": "excluded", "reason": "no_next_bar"}
    if not math.isfinite(float(event.get("atr", 0))) or float(event["atr"]) <= 0:
        return {"status": "excluded", "reason": "invalid_atr"}
    if fee_bps_per_side < 0 or slippage_bps_per_side < 0:
        raise ValueError("fee and slippage assumptions must be nonnegative")

    entry_time = int(event["entry_time_ms"])
    rows = [row for row in trade_segment if row["t"] >= entry_time][:MAX_HOLDING_BARS]
    if not rows or rows[0]["t"] != entry_time:
        return {"status": "excluded", "reason": "entry_bar_missing"}
    if len(rows) < MAX_HOLDING_BARS:
        return {"status": "excluded", "reason": "insufficient_matured_horizon"}
    for previous, current in zip(rows, rows[1:]):
        if current["t"] != previous["t"] + BAR_MS:
            return {"status": "excluded", "reason": "missing_bar_in_holding_horizon"}

    mark_rows = [mark_by_time.get(row["t"]) for row in rows]
    if any(row is None for row in mark_rows):
        return {"status": "excluded", "reason": "mark_price_bar_missing"}
    try:
        validate_mark_price_alignment(
            [_to_bar(row) for row in rows],
            [_to_bar(row, "mark_") for row in mark_rows],
        )
    except ValueError as exc:
        return {"status": "excluded", "reason": "mark_price_alignment_failed", "detail": str(exc)}

    side = event["side"]
    if side not in ("LONG", "SHORT"):
        return {"status": "excluded", "reason": "invalid_side"}
    direction = 1.0 if side == "LONG" else -1.0
    slip = slippage_bps_per_side / 10_000.0
    fee = fee_bps_per_side / 10_000.0

    # Entry is next trade-candle open with adverse execution slippage.
    entry_reference = float(rows[0]["o"])
    entry_fill = entry_reference * (1.0 + direction * slip)
    if not math.isfinite(entry_fill) or entry_fill <= 0:
        return {"status": "excluded", "reason": "invalid_entry_fill"}

    stop_distance = STOP_ATR * float(event["atr"])
    stop_price = entry_fill - direction * stop_distance
    target_price = entry_fill + direction * TARGET_ATR * float(event["atr"])
    if stop_price <= 0:
        return {"status": "excluded", "reason": "invalid_stop_price"}

    exit_bar = None
    exit_reference = None
    exit_reason = None
    for trade_bar, mark_bar in zip(rows, mark_rows):
        mark_open = float(mark_bar["mark_o"])
        mark_high = float(mark_bar["mark_h"])
        mark_low = float(mark_bar["mark_l"])

        if side == "LONG":
            stop_hit = mark_low <= stop_price
            target_hit = mark_high >= target_price
            stop_gap = mark_open <= stop_price
        else:
            stop_hit = mark_high >= stop_price
            target_hit = mark_low <= target_price
            stop_gap = mark_open >= stop_price

        # Conservative collision rule: stop always wins if both barriers touch.
        if stop_hit:
            exit_bar = trade_bar
            exit_reference = mark_open if stop_gap else stop_price
            exit_reason = "stop_loss"
            break
        if target_hit:
            exit_bar = trade_bar
            exit_reference = target_price
            exit_reason = "take_profit"
            break

    if exit_bar is None:
        exit_bar = rows[-1]
        exit_reference = float(mark_rows[-1]["mark_c"])
        exit_reason = "time_exit"

    exit_fill = float(exit_reference) * (1.0 - direction * slip)
    if not math.isfinite(exit_fill) or exit_fill <= 0:
        return {"status": "excluded", "reason": "invalid_exit_fill"}

    # Exclude, rather than model, every position whose open interval intersects
    # a Funding settlement. Include the exit candle close conservatively because
    # intrabar exit time is not observable from 15m OHLC.
    funding_during = [
        item for item in funding_events
        if entry_time <= item.funding_time_ms <= exit_bar["close_t"]
    ]
    if funding_during:
        return {
            "status": "excluded",
            "reason": "funding_settlement_during_open_position",
            "funding_events_during_position": len(funding_during),
        }

    gross_pnl_per_unit = direction * (exit_fill - entry_fill)
    entry_fee_per_unit = entry_fill * fee
    exit_fee_per_unit = exit_fill * fee
    net_pnl_per_unit = gross_pnl_per_unit - entry_fee_per_unit - exit_fee_per_unit
    net_r = net_pnl_per_unit / stop_distance
    if not math.isfinite(net_r):
        return {"status": "excluded", "reason": "non_finite_net_r"}

    return {
        "status": "labeled",
        "label": int(net_r > 0.0),
        "net_r": net_r,
        "gross_pnl_per_unit": gross_pnl_per_unit,
        "entry_fee_per_unit": entry_fee_per_unit,
        "exit_fee_per_unit": exit_fee_per_unit,
        "funding_cashflow_per_unit": 0.0,
        "funding_events_applied": 0,
        "entry_time_ms": entry_time,
        "exit_time_ms": int(exit_bar["close_t"]),
        "entry_fill": entry_fill,
        "exit_fill": exit_fill,
        "stop_price": stop_price,
        "target_price": target_price,
        "exit_reason": exit_reason,
        "bars_scanned": rows.index(exit_bar) + 1,
        "barrier_trigger_source": "MARK_PRICE_OHLC",
        "execution_price_proxy": "MARK_TRIGGER_LEVEL_WITH_ADVERSE_SLIPPAGE",
        "label_scope": "PRICE_ONLY_EXCLUDES_FUNDING_CROSSING_EVENTS_NO_MARGIN_OR_LIQUIDATION",
    }


def run(args) -> dict:
    drive_root = Path(args.drive_root).expanduser().resolve()
    raw_root = (drive_root / "raw-staging").resolve()
    output_root = (drive_root / "derived" / "price-only-labels").resolve()
    if not raw_root.is_dir():
        raise FileNotFoundError(f"raw-staging directory not found: {raw_root}")
    if raw_root == output_root or raw_root in output_root.parents:
        raise ValueError("output must remain outside raw-staging")
    if args.fee_bps_per_side < 0 or args.slippage_bps_per_side < 0:
        raise ValueError("fee and slippage assumptions must be nonnegative")

    symbols = list(SYMBOLS)
    funding_attestation, funding_status = load_funding_attestation(
        Path(args.funding_attestation), symbols
    )
    if funding_status != "VERIFIED":
        raise ValueError("Funding attestation/evidence hash/exact 240-cell grid did not verify")

    monthly_audits = []
    excluded_days = []
    source_hashes = []
    all_events = []
    labeled_events = []
    per_symbol_funding = {}
    per_symbol_segments = {}
    per_symbol_marks = {}

    for symbol in symbols:
        trade_by_time = {}
        mark_by_time = {}
        funding_rows = []
        symbol_segments = []

        for month in MONTHS:
            start, end = month_bounds(month)
            files = {
                "trade_15m": __import__("scripts.vike_exploratory_train", fromlist=["resolve_csv"]).resolve_csv(raw_root, month, symbol, "trade_15m"),
                "mark_15m": __import__("scripts.vike_exploratory_train", fromlist=["resolve_csv"]).resolve_csv(raw_root, month, symbol, "mark_15m"),
                "funding": __import__("scripts.vike_exploratory_train", fromlist=["resolve_csv"]).resolve_csv(raw_root, month, symbol, "funding"),
            }
            for stream, path in files.items():
                digest = sha256_file(path)
                source_hashes.append({
                    "symbol": symbol, "month": month, "stream": stream,
                    "path_relative": str(path.relative_to(drive_root)),
                    "sha256": digest, "bytes": path.stat().st_size,
                })

            expected_funding = funding_attestation[(symbol, month)]["funding_file_sha256"]
            actual_funding = next(
                row["sha256"] for row in source_hashes
                if row["symbol"] == symbol and row["month"] == month and row["stream"] == "funding"
            )
            if actual_funding != expected_funding:
                raise ValueError(f"{symbol}/{month}: local Funding CSV SHA-256 differs from verified attestation")

            trade = read_trade_or_mark(files["trade_15m"], "trade_15m", start, end)
            mark = read_trade_or_mark(files["mark_15m"], "mark_15m", start, end)
            funding = _read_funding(files["funding"], start, end)
            eligible, excluded = complete_day_bars(trade, mark, start, end)
            excluded_days.extend({"symbol": symbol, "month": month, **item} for item in excluded)

            for row in eligible:
                trade_by_time[row["t"]] = row
                mark_row = mark[row["t"]]
                mark_by_time[row["t"]] = {
                    "t": row["t"], "close_t": mark_row["close_t"],
                    "mark_o": mark_row["o"], "mark_h": mark_row["h"],
                    "mark_l": mark_row["l"], "mark_c": mark_row["c"],
                }
            funding_rows.extend(funding)
            monthly_audits.append({
                "symbol": symbol, "month": month,
                "trade_rows": len(trade), "mark_rows": len(mark),
                "funding_rows": len(funding),
                "funding_sha256": actual_funding,
                "eligible_price_days": len(eligible) // 96,
                "excluded_days": excluded,
                "funding_hash_status": "VERIFIED",
                "maintenance_brackets_status": "NOT_USED_PRICE_ONLY",
            })

        ordered = [trade_by_time[t] for t in sorted(trade_by_time)]
        segments = split_contiguous(ordered)
        symbol_segments[symbol] = segments
        per_symbol_marks[symbol] = mark_by_time
        per_symbol_funding[symbol] = sorted(funding_rows, key=lambda item: item.funding_time_ms)
        for segment in segments:
            all_events.extend(build_opportunity_events(symbol, segment))

    all_events = apply_overlap_control(all_events, MAX_HOLDING_BARS)
    segment_lookup = {}
    for symbol, segments in symbol_segments.items():
        for segment in segments:
            for row in segment:
                segment_lookup[(symbol, row["close_t"])] = segment

    for event in all_events:
        segment = segment_lookup.get((event["symbol"], event["signal_time_ms"]))
        if segment is None:
            result = {"status": "excluded", "reason": "event_segment_not_found"}
        else:
            result = label_price_only_event(
                event, segment, per_symbol_marks[event["symbol"]],
                per_symbol_funding[event["symbol"]],
                args.fee_bps_per_side, args.slippage_bps_per_side,
            )
        event["label_status"] = "LABELED_PRICE_ONLY" if result["status"] == "labeled" else "EXCLUDED"
        event["label_reason"] = result.get("reason")
        event["label_detail"] = result.get("detail")
        if result["status"] == "labeled":
            for key in (
                "label", "net_r", "gross_pnl_per_unit", "entry_fee_per_unit",
                "exit_fee_per_unit", "funding_cashflow_per_unit", "funding_events_applied",
                "exit_time_ms", "entry_fill", "exit_fill", "stop_price", "target_price",
                "exit_reason", "bars_scanned", "barrier_trigger_source",
                "execution_price_proxy", "label_scope",
            ):
                event[key] = result[key]
            labeled_events.append(event)

    output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=False, exist_ok=False)

    per_symbol = {}
    for symbol in symbols:
        rows = [row for row in all_events if row["symbol"] == symbol]
        per_symbol[symbol] = {
            "events": len(rows),
            "long_events": sum(row["side"] == "LONG" for row in rows),
            "short_events": sum(row["side"] == "SHORT" for row in rows),
            "labeled_price_only": sum(row["label_status"] == "LABELED_PRICE_ONLY" for row in rows),
            "excluded": sum(row["label_status"] != "LABELED_PRICE_ONLY" for row in rows),
            "label_balance": {
                "negative": sum(row.get("label") == 0 for row in rows),
                "positive": sum(row.get("label") == 1 for row in rows),
            },
            "exclusion_reasons": dict(Counter(
                row.get("label_reason") or row.get("overlap_status")
                for row in rows if row["label_status"] != "LABELED_PRICE_ONLY"
            )),
        }

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": "PRICE_ONLY_LABELS_PREPARED_NOT_MODEL_APPROVAL",
        "label_scope": "PRICE_ONLY_EXCLUDES_FUNDING_CROSSING_EVENTS_NO_MARGIN_OR_LIQUIDATION",
        "venue": "Binance USD-M",
        "market_type": "USDT perpetual futures",
        "timeframe": "15m",
        "symbols": symbols,
        "development_months": list(MONTHS),
        "locked_holdout_months_accessed": False,
        "raw_data_mutated": False,
        "model_training_started": False,
        "funding_attestation_status": "VERIFIED",
        "funding_local_csv_hashes_match": True,
        "maintenance_brackets_status": "NOT_VERIFIED_NOT_USED",
        "event_generator": "DONCHIAN_20_DIRECTION_NEUTRAL_CLOSED_15M",
        "overlap_policy": "FIXED_48_BAR_PER_SYMBOL_COOLDOWN_OUTCOME_INDEPENDENT",
        "entry_policy": "NEXT_15M_TRADE_BAR_OPEN_WITH_ADVERSE_SLIPPAGE",
        "barrier_policy": "MARK_PRICE_OHLC_STOP_1_5_ATR_TARGET_3_ATR_STOP_FIRST_MAX_48_BARS",
        "time_exit_policy": "48TH_MARK_PRICE_BAR_CLOSE",
        "cost_policy": {
            "fee_bps_per_side": args.fee_bps_per_side,
            "slippage_bps_per_side": args.slippage_bps_per_side,
            "funding_cashflow_applied": False,
        },
        "funding_policy": "EXCLUDE_ANY_EVENT_WITH_FUNDING_SETTLEMENT_FROM_ENTRY_TIME_THROUGH_EXIT_BAR_CLOSE",
        "net_r_definition": "trade_directional_price_pnl_after_round_trip_taker_fees_and_adverse_slippage_divided_by_1_5_atr; funding_cashflows_excluded",
        "maintenance_brackets_used": False,
        "margin_or_leverage_used": False,
        "liquidation_model_used": False,
        "event_count": len(all_events),
        "labeled_event_count": len(labeled_events),
        "per_symbol": per_symbol,
        "acceptance_status": "NOT_EVALUATED_NO_MODEL_TRAINING",
        "acceptance_blockers": [
            "Price-only labels do not model funding cashflows; events overlapping settlements are excluded.",
            "No maintenance-margin, isolated-margin, leverage, or liquidation behavior is modeled.",
            "A separate research protocol is required before Logistic Regression/WFO model acceptance.",
        ],
        "source_file_hashes": source_hashes,
    }

    (output_dir / "research_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (output_dir / "monthly_audits.json").write_text(
        json.dumps(monthly_audits, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (output_dir / "excluded_days.json").write_text(
        json.dumps(excluded_days, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    fields = [
        "symbol", "signal_time_ms", "entry_time_ms", "side", "atr",
        "overlap_status", "label_status", "label_reason", "label", "net_r",
        "exit_time_ms", "exit_reason", "entry_fill", "exit_fill",
        "stop_price", "target_price", "funding_events_applied", *FEATURES,
    ]
    with (output_dir / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for event in sorted(all_events, key=lambda row: (row["signal_time_ms"], row["symbol"])):
            writer.writerow({**event, **event["features"]})
    (output_dir / "dataset_manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "run_id": run_id,
            "label_scope": report["label_scope"],
            "symbols": symbols,
            "months": list(MONTHS),
            "source_file_hashes": source_hashes,
            "funding_attestation_sha256": sha256_file(Path(args.funding_attestation)),
            "model_training_started": False,
            "locked_holdout_accessed": False,
            "raw_data_mutated": False,
        }, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {"output_dir": str(output_dir), "report": report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-root", required=True)
    parser.add_argument("--funding-attestation", required=True, type=Path)
    parser.add_argument("--fee-bps-per-side", type=float, default=FEE_BPS_PER_SIDE)
    parser.add_argument("--slippage-bps-per-side", type=float, default=SLIPPAGE_BPS_PER_SIDE)
    args = parser.parse_args()
    try:
        result = run(args)
        report = result["report"]
        print(json.dumps({
            "output_dir": result["output_dir"],
            "verdict": report["verdict"],
            "events": report["event_count"],
            "labeled": report["labeled_event_count"],
            "maintenance": report["maintenance_brackets_status"],
            "model_training_started": report["model_training_started"],
        }, indent=2))
        return 0
    except Exception as exc:
        parser.exit(2, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
