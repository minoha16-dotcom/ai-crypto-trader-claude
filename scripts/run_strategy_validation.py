"""Run a reproducible, pre-registered multi-market walk-forward research report.

This is a paper-only historical evaluation. It fetches public OHLCV data,
excludes the still-forming candle, rejects gaps, and writes JSON + Markdown.
It does not optimize or choose a strategy based on the test results.
"""
import asyncio
import json
from collections import Counter
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# A direct "python scripts/run_strategy_validation.py" invocation places
# scripts/ on sys.path. Add the repository root so backend imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings
from backend.exchanges import ExchangeRegistry
from backend.walk_forward import run_walk_forward_backtest


DEFAULT_TIMEFRAMES = ("1h", "4h")
TIMEFRAME_MS_BY_INTERVAL = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}
DEFAULT_EXCHANGES = ("kucoin", "okx")
SPOT_SYMBOLS = ("BTC/USDT", "ETH/USDT")
FUTURES_SYMBOLS = ("BTC/USDT", "ETH/USDT")
VOLUME_THRESHOLDS = (0.8, 1.0, 1.2)
ABLATIONS = (
    "baseline",
    "without_ema_alignment",
    "without_price_vs_ema20",
    "single_composite_trend",
)
LIMIT = 5000
MINIMUM_CANDLES = 50 + 250 + 100 + 100 + 2 * 50
STARTING_BALANCE = 10_000.0
WALK_FORWARD = {
    "train_bars": 250,
    "validation_bars": 100,
    "test_bars": 100,
    "step_bars": 100,
    "warmup_bars": 50,
    "purge_bars": 20,
    "embargo_bars": 50,
    "max_holding_bars": 20,
    "window_mode": "expanding",
    "max_folds": 45,
}


def _parse_csv(name, default):
    value = os.getenv(name, "")
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    return tuple(items) if items else tuple(default)


def _parse_timeframes():
    raw = os.getenv("STRATEGY_VALIDATION_TIMEFRAMES", "")
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    timeframes = tuple(dict.fromkeys(values or DEFAULT_TIMEFRAMES))
    unsupported = sorted(set(timeframes) - set(TIMEFRAME_MS_BY_INTERVAL))
    if unsupported:
        raise ValueError(
            "unsupported strategy-validation timeframe(s): "
            + ", ".join(unsupported)
            + ". Supported: "
            + ", ".join(TIMEFRAME_MS_BY_INTERVAL)
        )
    return timeframes


def _iso_utc(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _wilson_interval(successes, sample_size):
    """95% Wilson interval for the positive-R share."""
    if sample_size <= 0:
        return None
    successes = max(0, min(int(successes), int(sample_size)))
    n = float(sample_size)
    p = successes / n
    z = 1.959963984540054
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    radius = (
        z * math.sqrt((p * (1.0 - p) / n) + z2 / (4.0 * n * n))
        / denominator
    )
    return [
        round(max(0.0, center - radius), 6),
        round(min(1.0, center + radius), 6),
    ]


def _benchmark_return(candles, start, end):
    """Long buy-and-hold comparator with round-trip fee/slippage estimate."""
    if end - start < 2:
        return 0.0
    entry = float(candles[start + 1][1])
    exit_price = float(candles[end - 1][4])
    if entry <= 0:
        return 0.0
    round_trip_cost_pct = 2.0 * (
        settings.taker_fee_rate + settings.slippage_bps / 10_000.0
    ) * 100.0
    return (exit_price / entry - 1.0) * 100.0 - round_trip_cost_pct


def _summarize(result, candles):
    aggregate = result["aggregate_test"]
    test_folds = []
    excluded_reasons = Counter()
    eligible_samples = 0
    excluded_samples = 0
    eligible_r_sum = 0.0
    positive_r_count = 0
    oos_signals = 0
    for fold in result["folds"]:
        test = fold["test"]
        span = fold["ranges"]["test"]
        test_folds.append({
            "fold": fold["fold"],
            "start": span["start_timestamp"],
            "end": span["end_timestamp"],
            "test_candles": span["candle_count"],
            "closed_trades": test["closed_trades"],
            "net_return_pct": test["net_return_pct"],
            "expectancy_r": test["expectancy_r"],
            "profit_factor": test["profit_factor"],
            "max_drawdown_pct": test["max_drawdown_pct"],
            "buy_hold_return_pct": _benchmark_return(
                candles, span["start_index"], span["end_index_exclusive"]
            ),
            "oos_signals": test["oos_signals"],
            "eligible_samples": test["eligible_samples"],
            "excluded_samples": test["excluded_samples"],
            "eligible_r_sum": test["eligible_r_sum"],
            "positive_r_count": test["positive_r_count"],
            "excluded_sample_reasons": test["excluded_sample_reasons"],
        })
        eligible_samples += int(test["eligible_samples"])
        excluded_samples += int(test["excluded_samples"])
        eligible_r_sum += float(test["eligible_r_sum"])
        positive_r_count += int(test["positive_r_count"])
        oos_signals += int(test["oos_signals"])
        for reason, count in test["excluded_sample_reasons"].items():
            excluded_reasons[reason] += int(count)

    strategy_returns = [row["net_return_pct"] for row in test_folds]
    benchmark_returns = [row["buy_hold_return_pct"] for row in test_folds]
    return {
        "fold_count": result["fold_count"],
        "total_closed_trades": aggregate["total_closed_trades"],
        "winning_trades": aggregate["winning_trades"],
        "losing_trades": aggregate["losing_trades"],
        "breakeven_trades": aggregate["breakeven_trades"],
        "win_rate_pct": aggregate["trade_weighted_win_rate_pct"],
        "mean_fold_net_return_pct": statistics.fmean(strategy_returns) if strategy_returns else 0.0,
        "median_fold_net_return_pct": statistics.median(strategy_returns) if strategy_returns else 0.0,
        "positive_test_folds": sum(value > 0 for value in strategy_returns),
        "positive_test_fold_pct": (
            sum(value > 0 for value in strategy_returns) / len(strategy_returns) * 100.0
            if strategy_returns else 0.0
        ),
        "mean_buy_hold_return_pct": statistics.fmean(benchmark_returns) if benchmark_returns else 0.0,
        "mean_excess_vs_buy_hold_pct": (
            statistics.fmean(strategy_returns) - statistics.fmean(benchmark_returns)
            if strategy_returns and benchmark_returns else 0.0
        ),
        "expectancy_r": aggregate["expectancy_r"],
        "profit_factor": aggregate["profit_factor"],
        "worst_fold_max_drawdown_pct": aggregate["worst_fold_max_drawdown_pct"],
        "sum_of_independent_fold_net_pnl": aggregate["sum_of_independent_fold_net_pnl"],
        "eligible_samples": eligible_samples,
        "excluded_samples": excluded_samples,
        "excluded_sample_reasons": dict(sorted(excluded_reasons.items())),
        "oos_signals": oos_signals,
        "mean_r": eligible_r_sum / eligible_samples if eligible_samples else None,
        "positive_r_share": positive_r_count / eligible_samples if eligible_samples else None,
        "wilson_ci_positive_r_share_95": _wilson_interval(positive_r_count, eligible_samples),
        "test_folds": test_folds,
    }


async def _fetch_dataset(
    registry, exchange_id, market_type, requested_symbol, timeframe, timeframe_ms, limit
):
    spec = await registry.market_spec(exchange_id, requested_symbol, market_type)
    candles, metadata = await registry.ohlcv_with_metadata(
        exchange_id, spec["symbol"], timeframe, limit, market_type
    )
    # CCXT may include the still-forming candle. Never score it.
    now_ms = int(time.time() * 1000)
    closed = [
        row for row in candles
        if int(row[0]) + timeframe_ms <= now_ms
    ]
    if len(closed) < MINIMUM_CANDLES:
        raise ValueError(
            f"insufficient closed candles: received={len(closed)}, "
            f"minimum={MINIMUM_CANDLES}, metadata={metadata}"
        )
    return spec, closed, metadata



def _align_group_to_common_utc(group_rows, requested_timeframes, min_candles=MINIMUM_CANDLES):
    """Trim an exchange/symbol/market group to one exact UTC comparison window.

    OHLCV timestamps are candle-open times in UTC milliseconds. The common
    interval is rounded inward to a boundary shared by every requested
    timeframe, then only fully closed candles in that interval are retained.
    """
    requested_timeframes = tuple(dict.fromkeys(requested_timeframes))
    group_id = group_rows[0].get("comparison_group_id") if group_rows else "unknown"
    by_timeframe = {}
    for row in group_rows:
        timeframe = row["timeframe"]
        if timeframe in by_timeframe:
            return {
                "comparison_group_id": group_id,
                "timeframes": list(requested_timeframes),
                "status": "REJECTED_DUPLICATE_TIMEFRAME",
                "reason": f"More than one dataset was supplied for timeframe {timeframe}.",
            }, []
        by_timeframe[timeframe] = row

    missing = [frame for frame in requested_timeframes if frame not in by_timeframe]
    failed = [
        f"{row['timeframe']}: {row.get('error') or 'no candle data'}"
        for row in group_rows if row.get("error") or not row.get("candles")
    ]
    if missing or failed:
        details = []
        if missing:
            details.append("missing requested timeframe(s): " + ", ".join(missing))
        if failed:
            details.append("failed dataset(s): " + "; ".join(failed))
        return {
            "comparison_group_id": group_id,
            "timeframes": list(requested_timeframes),
            "status": "REJECTED_MISSING_TIMEFRAME_DATA",
            "reason": " | ".join(details),
        }, []

    if len(by_timeframe) != len(requested_timeframes):
        return {
            "comparison_group_id": group_id,
            "timeframes": list(requested_timeframes),
            "status": "REJECTED_INCOMPLETE_TIMEFRAME_SET",
            "reason": "The group does not contain exactly one successful dataset for every requested timeframe.",
        }, []

    widths = [TIMEFRAME_MS_BY_INTERVAL[frame] for frame in requested_timeframes]
    common_step_ms = math.lcm(*widths)
    available_start_ms = max(
        int(by_timeframe[frame]["candles"][0][0])
        for frame in requested_timeframes
    )
    available_end_ms = min(
        int(by_timeframe[frame]["candles"][-1][0]) + TIMEFRAME_MS_BY_INTERVAL[frame]
        for frame in requested_timeframes
    )
    common_start_ms = ((available_start_ms + common_step_ms - 1) // common_step_ms) * common_step_ms
    common_end_ms = (available_end_ms // common_step_ms) * common_step_ms

    base = {
        "comparison_group_id": group_id,
        "timeframes": list(requested_timeframes),
        "common_timeframe_boundary_ms": common_step_ms,
        "available_start_utc": _iso_utc(available_start_ms),
        "available_end_utc": _iso_utc(available_end_ms),
        "common_window_start_utc": _iso_utc(common_start_ms),
        "common_window_end_utc": _iso_utc(common_end_ms),
        "common_window_start_ms": common_start_ms,
        "common_window_end_ms": common_end_ms,
        "minimum_candles_per_timeframe": min_candles,
    }
    if common_end_ms <= common_start_ms:
        return {
            **base,
            "status": "REJECTED_NO_COMMON_UTC_WINDOW",
            "reason": "The requested timeframes have no positive common UTC interval after boundary alignment.",
        }, []

    aligned_rows = []
    coverage_details = []
    problems = []
    for timeframe in requested_timeframes:
        row = by_timeframe[timeframe]
        timeframe_ms = TIMEFRAME_MS_BY_INTERVAL[timeframe]
        raw_candles = row["candles"]
        timestamps = [int(candle[0]) for candle in raw_candles]
        if any(timestamp % timeframe_ms != 0 for timestamp in timestamps):
            problems.append(f"{timeframe}: candle timestamps are not aligned to UTC timeframe boundaries")
            continue
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            problems.append(f"{timeframe}: candle timestamps are unsorted or duplicated")
            continue

        clipped = [
            candle for candle in raw_candles
            if int(candle[0]) >= common_start_ms
            and int(candle[0]) + timeframe_ms <= common_end_ms
        ]
        expected = (common_end_ms - common_start_ms) // timeframe_ms
        if len(clipped) < min_candles:
            problems.append(
                f"{timeframe}: only {len(clipped)} complete candles remain in the common window; "
                f"minimum required is {min_candles}"
            )
            continue
        if len(clipped) != expected:
            problems.append(
                f"{timeframe}: common window contains {expected - len(clipped)} missing candle(s); "
                "gaps are not interpolated or forward-filled"
            )
            continue
        clipped_timestamps = [int(candle[0]) for candle in clipped]
        if any(right - left != timeframe_ms for left, right in zip(clipped_timestamps, clipped_timestamps[1:])):
            problems.append(f"{timeframe}: missing or irregular candle timestamps inside the common UTC window")
            continue

        aligned_row = dict(row)
        aligned_row["candles"] = clipped
        aligned_row["candle_count_before_alignment"] = len(raw_candles)
        aligned_row["candle_count"] = len(clipped)
        aligned_row["first_candle_utc"] = _iso_utc(clipped[0][0])
        aligned_row["last_candle_utc"] = _iso_utc(clipped[-1][0])
        aligned_row["last_candle_close_utc"] = _iso_utc(int(clipped[-1][0]) + timeframe_ms)
        aligned_row["common_window_start_utc"] = base["common_window_start_utc"]
        aligned_row["common_window_end_utc"] = base["common_window_end_utc"]
        aligned_row["coverage_pct"] = len(clipped) / expected * 100.0 if expected else 0.0
        aligned_rows.append(aligned_row)
        coverage_details.append({
            "dataset_id": row["id"],
            "timeframe": timeframe,
            "candles_before_alignment": len(raw_candles),
            "candles_in_common_window": len(clipped),
            "expected_candles": expected,
            "coverage_pct": round(aligned_row["coverage_pct"], 4),
            "actual_start_utc": aligned_row["first_candle_utc"],
            "actual_end_utc": aligned_row["last_candle_close_utc"],
        })

    if problems:
        status = (
            "REJECTED_INSUFFICIENT_COMMON_COVERAGE"
            if any("minimum required" in problem or "missing candle" in problem for problem in problems)
            else "REJECTED_INVALID_OR_GAPPED_DATA"
        )
        return {
            **base,
            "status": status,
            "reason": " | ".join(problems),
            "dataset_coverage": coverage_details,
        }, []

    status = (
        "SINGLE_TIMEFRAME_NO_CROSS_TIMEFRAME_COMPARISON"
        if len(requested_timeframes) == 1 else "ALIGNED_COMMON_UTC"
    )
    return {
        **base,
        "status": status,
        "reason": None,
        "common_duration_days": round((common_end_ms - common_start_ms) / 86_400_000, 4),
        "dataset_coverage": coverage_details,
    }, aligned_rows


def _dataset_report_row(row, comparison_status, evaluation_status, reason=None):
    timeframe_ms = TIMEFRAME_MS_BY_INTERVAL[row["timeframe"]]
    candles = row.get("candles") or []
    first = candles[0] if candles else None
    last = candles[-1] if candles else None
    return {
        "id": row["id"],
        "comparison_group_id": row["comparison_group_id"],
        "exchange": row["exchange"],
        "symbol": row.get("symbol") or row.get("requested_symbol"),
        "requested_symbol": row["requested_symbol"],
        "market_type": row["market_type"],
        "timeframe": row["timeframe"],
        "contract_type": (row.get("instrument") or {}).get("contract_type"),
        "contract_size": (row.get("instrument") or {}).get("contract_size"),
        "candle_count_before_alignment": row.get("candle_count_before_alignment", len(candles)),
        "candle_count": row.get("candle_count", len(candles)),
        "first_candle_utc": row.get("first_candle_utc") or (_iso_utc(first[0]) if first else None),
        "last_candle_utc": row.get("last_candle_utc") or (_iso_utc(last[0]) if last else None),
        "last_candle_close_utc": row.get("last_candle_close_utc") or (
            _iso_utc(int(last[0]) + timeframe_ms) if last else None
        ),
        "common_window_start_utc": row.get("common_window_start_utc"),
        "common_window_end_utc": row.get("common_window_end_utc"),
        "coverage_pct": row.get("coverage_pct"),
        "source_metadata": row.get("source_metadata"),
        "comparison_status": comparison_status,
        "evaluation_status": evaluation_status,
        "status_reason": reason or row.get("error"),
    }


def _build_fold_isolated_advisor_model(evaluated, dataset, candles, timeframe_ms, backtest_config):
    """Fit only on each fold's past, then score that fold's untouched test rows."""
    import hashlib

    from backend.advisor_approval import evaluate_ranking_approval
    from backend.advisor_model import (
        DEFAULT_ALPHA,
        FEATURE_SCHEMA_VERSION,
        MODEL_VERSION,
        fit_ridge,
        predict_r_multiple,
    )

    group_id = dataset["id"]
    fold_records = []
    oos_rows = []
    fold_models = []
    for fold in evaluated.get("folds", []):
        fold_id = f"{group_id}:fold:{fold['fold']}"
        train_rows = (
            list(fold["train"].get("advisor_samples", []))
            + list(fold["validation"].get("advisor_samples", []))
        )
        test_rows = list(fold["test"].get("advisor_samples", []))
        test_range = fold["ranges"]["test"]
        validation_range = fold["ranges"]["validation"]
        test_start_ms = int(test_range["start_timestamp"])
        test_end_ms = int(test_range["end_timestamp"]) + int(timeframe_ms)
        train_end_ms = int(validation_range["end_timestamp"]) + int(timeframe_ms)
        oos_signals = int(fold["test"].get("oos_signals", 0))

        record = {
            "fold_id": fold_id,
            "training_eligible_samples": len(train_rows),
            "test_eligible_samples": len(test_rows),
            "oos_signals": oos_signals,
            "train_end_ms": train_end_ms,
            "test_start_ms": test_start_ms,
            "test_end_ms": test_end_ms,
            "model_fit_success": False,
        }
        if len(train_rows) < 200 or len(test_rows) < 20 or oos_signals < 20:
            record["model_fit_reason"] = "FOLD_BELOW_MINIMUM_TRAIN_OR_TEST_SAMPLE_GATE"
            fold_records.append(record)
            continue

        try:
            model = fit_ridge(train_rows, alpha=DEFAULT_ALPHA)
            predictions = []
            for index, sample in enumerate(test_rows):
                predicted = predict_r_multiple(model, sample)
                predictions.append({
                    "group_id": group_id,
                    "fold_id": fold_id,
                    "sample_id": f"{fold_id}:{sample['signal_timestamp_ms']}:{index}",
                    "signal_timestamp_ms": int(sample["signal_timestamp_ms"]),
                    "r_multiple": float(sample["r_multiple"]),
                    "predicted_r_multiple": predicted,
                })
            oos_rows.extend(predictions)
            record["model_fit_success"] = True
            record["model_fit_reason"] = None
            fold_models.append({
                "fold_id": fold_id,
                "model_version": model["model_version"],
                "feature_schema_version": model["feature_schema_version"],
                "algorithm": model["algorithm"],
                "alpha": model["alpha"],
                "training_samples": model["training_samples"],
                "intercept": model["intercept"],
                "weights": model["weights"],
                "feature_names": model["feature_names"],
            })
        except (ValueError, TypeError, OverflowError) as error:
            record["model_fit_reason"] = f"MODEL_FIT_OR_PREDICTION_ERROR: {type(error).__name__}: {error}"
        fold_records.append(record)

    digest_payload = {
        "ohlcv": candles,
        "source_metadata": dataset.get("source_metadata"),
        "instrument": dataset.get("instrument"),
    }
    dataset_sha256 = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    metadata = {
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "algorithm": "ridge_regression_l2",
        "hyperparameters": {
            "alpha": DEFAULT_ALPHA,
            "features": [
                "directional_composite_trend",
                "directional_rsi",
                "adx_strength",
                "log_volume_ratio",
                "log_atr_percent",
            ],
            "target": "net_r_multiple",
        },
        "backtest_config": backtest_config,
        "data_source": {
            "provider": "ccxt_exchange_registry",
            "exchange": dataset["exchange"],
            "symbol": dataset["symbol"],
            "market_type": dataset["market_type"],
            "timeframe": dataset["timeframe"],
            "source_metadata": dataset.get("source_metadata"),
        },
        "dataset_sha256": dataset_sha256,
        "period_utc": {
            "start": dataset.get("common_window_start_utc"),
            "end": dataset.get("common_window_end_utc"),
        },
        "group_id": group_id,
        "validation_method": "expanding_chronological_walk_forward",
        "code_sha": os.getenv("GITHUB_SHA", "local"),
    }
    approval = evaluate_ranking_approval(
        metadata=metadata,
        fold_records=fold_records,
        oos_rows=oos_rows,
    )
    approval["fold_models"] = fold_models
    approval["data_period_utc"] = metadata["period_utc"]
    approval["dataset_sha256"] = dataset_sha256
    approval["prediction_count"] = len(oos_rows)
    approval["runtime_activation"] = "NOT_CONNECTED_TO_RUNTIME_ADVISOR"
    return approval


def _build_advisor_diagnostics(dataset_rows, results):
    """Build per-dataset R diagnostics and auditable model approval status."""
    baseline_by_dataset = {
        result["dataset_id"]: result
        for result in results
        if result["volume_ratio_threshold"] == 1.0
        and result["feature_ablation"] == "baseline"
    }
    diagnostics = []
    for dataset in dataset_rows:
        result = baseline_by_dataset.get(dataset["id"])
        metrics = result.get("metrics") if result else None
        approval = result.get("advisor_model_approval") if result else None
        if metrics:
            eligible = int(metrics.get("eligible_samples", 0))
            excluded = int(metrics.get("excluded_samples", 0))
            reasons = metrics.get("excluded_sample_reasons", {})
            oos_signals = int(metrics.get("oos_signals", 0))
            oos_trades = int(metrics.get("total_closed_trades", 0))
            folds = int(metrics.get("fold_count", 0))
            mean_r = metrics.get("mean_r")
            positive_share = metrics.get("positive_r_share")
            wilson = metrics.get("wilson_ci_positive_r_share_95")
            feature_excluded = int(metrics.get("model_feature_excluded_samples", 0))
            feature_excluded_reasons = metrics.get("model_feature_excluded_reasons", {})
            diagnostic_status = "AVAILABLE"
        else:
            eligible = excluded = oos_signals = oos_trades = folds = feature_excluded = 0
            reasons = {}
            feature_excluded_reasons = {}
            mean_r = positive_share = wilson = None
            diagnostic_status = "UNAVAILABLE_NO_COMPLETED_BASELINE_EVALUATION"

        if approval:
            model_status = approval["model_status"]
            approval_reasons = approval["approval_reasons"]
            training_samples = approval["maximum_training_eligible_samples"]
            min_training_samples = approval["minimum_training_samples_across_valid_folds"]
            valid_folds = approval["valid_walk_forward_folds"]
            model_oos_signals = approval["oos_signals"]
            model_oos_eligible = approval["oos_eligible_samples"]
            top_tercile_mean_r = approval["top_tercile_mean_r"]
            all_oos_mean_r = approval["all_oos_mean_r"]
            uplift = approval["uplift"]
            uplift_ci = approval["uplift_confidence_interval_95"]
            model_version = approval["model_metadata"]["model_version"]
            model_metadata = approval["model_metadata"]
        else:
            model_status = "DISABLED"
            approval_reasons = ["NO_COMPLETED_BASELINE_EVALUATION"]
            training_samples = min_training_samples = valid_folds = 0
            model_oos_signals = model_oos_eligible = 0
            top_tercile_mean_r = all_oos_mean_r = uplift = uplift_ci = None
            model_version = None
            model_metadata = None

        diagnostics.append({
            "dataset_id": dataset["id"],
            "comparison_group_id": dataset["comparison_group_id"],
            "exchange": dataset["exchange"],
            "symbol": dataset["symbol"],
            "market_type": dataset["market_type"],
            "timeframe": dataset["timeframe"],
            "common_window_start_utc": dataset.get("common_window_start_utc"),
            "common_window_end_utc": dataset.get("common_window_end_utc"),
            "candle_count": dataset.get("candle_count", 0),
            "walk_forward_folds": folds,
            "oos_signals": oos_signals,
            "oos_closed_trades": oos_trades,
            "eligible_samples": eligible,
            "eligible_samples_scope": "eligible closed trades with valid net R in chronological OOS test folds",
            "excluded_samples": excluded,
            "excluded_sample_reasons": reasons,
            "model_feature_excluded_samples": feature_excluded,
            "model_feature_excluded_reasons": feature_excluded_reasons,
            "mean_r": mean_r,
            "positive_r_share": positive_share,
            "wilson_ci_positive_r_share_95": wilson,
            "training_eligible_samples_max": training_samples,
            "training_eligible_samples_min_valid_fold": min_training_samples,
            "valid_walk_forward_folds": valid_folds,
            "model_oos_signals": model_oos_signals,
            "model_oos_eligible_samples": model_oos_eligible,
            "top_tercile_mean_r": top_tercile_mean_r,
            "all_oos_mean_r": all_oos_mean_r,
            "uplift": uplift,
            "uplift_confidence_interval_95": uplift_ci,
            "model_version": model_version,
            "model_metadata": model_metadata,
            "approval_reasons": approval_reasons,
            "diagnostic_status": diagnostic_status,
            "model_status": model_status,
            "model_status_scope": "per-group R-multiple walk-forward ranker",
            "runtime_advisor_status": "LEGACY_LOCAL_PAPER_TRADE_SIMILARITY",
            "runtime_activation": "NOT_CONNECTED_TO_RUNTIME_ADVISOR",
        })
    return diagnostics




async def run():
    timeframes = _parse_timeframes()
    exchanges = _parse_csv("STRATEGY_VALIDATION_EXCHANGES", DEFAULT_EXCHANGES)
    allowed = {"kucoin", "bybit", "okx"}
    unknown = sorted(set(exchanges) - allowed)
    if unknown:
        raise ValueError("unsupported exchange(s): " + ", ".join(unknown))
    limit = int(os.getenv("STRATEGY_VALIDATION_LIMIT", str(LIMIT)))
    if not MINIMUM_CANDLES <= limit <= 5000:
        raise ValueError(
            f"STRATEGY_VALIDATION_LIMIT must be between {MINIMUM_CANDLES} and 5000"
        )

    registry = ExchangeRegistry()
    raw_groups = {}
    errors = []
    results = []
    comparison_groups = []
    dataset_reports = []
    try:
        for exchange_id in exchanges:
            requests = (
                [("spot", symbol) for symbol in SPOT_SYMBOLS]
                + [("futures", symbol) for symbol in FUTURES_SYMBOLS]
            )
            for market_type, requested_symbol in requests:
                group_id = f"{exchange_id}:{market_type}:{requested_symbol}"
                group_rows = []
                for timeframe in timeframes:
                    timeframe_ms = TIMEFRAME_MS_BY_INTERVAL[timeframe]
                    dataset_id = f"{exchange_id}:{timeframe}:{market_type}:{requested_symbol}"
                    row = {
                        "id": dataset_id,
                        "comparison_group_id": group_id,
                        "exchange": exchange_id,
                        "timeframe": timeframe,
                        "market_type": market_type,
                        "requested_symbol": requested_symbol,
                        "candles": None,
                        "instrument": None,
                        "source_metadata": None,
                        "error": None,
                    }
                    try:
                        spec, candles, source_meta = await _fetch_dataset(
                            registry, exchange_id, market_type, requested_symbol,
                            timeframe, timeframe_ms, limit
                        )
                        row.update({
                            "symbol": spec["symbol"],
                            "candles": candles,
                            "instrument": spec,
                            "source_metadata": source_meta,
                        })
                        print(
                            f"FETCHED {dataset_id}: {len(candles)} closed candles "
                            f"from {_iso_utc(candles[0][0])} to "
                            f"{_iso_utc(int(candles[-1][0]) + timeframe_ms)}"
                        )
                    except Exception as error:
                        row["error"] = f"{type(error).__name__}: {error}"
                        errors.append({"dataset_id": dataset_id, "error": row["error"]})
                        print(f"INCOMPLETE {dataset_id}: {row['error']}")
                    group_rows.append(row)
                raw_groups[group_id] = group_rows
    finally:
        await registry.close()

    for group_id, group_rows in raw_groups.items():
        alignment, aligned_rows = _align_group_to_common_utc(
            group_rows, timeframes, min_candles=MINIMUM_CANDLES
        )
        if not aligned_rows:
            comparison_groups.append(alignment)
            reason = alignment.get("reason") or alignment["status"]
            for row in group_rows:
                dataset_reports.append(
                    _dataset_report_row(row, alignment["status"], "NOT_RUN_COMMON_WINDOW_REJECTED", reason)
                )
            errors.append({
                "comparison_group_id": group_id,
                "error": f"{alignment['status']}: {reason}",
            })
            print(f"REJECTED {group_id}: {alignment['status']} — {reason}")
            continue

        group_results = []
        group_dataset_reports = []
        group_error = None
        for row in aligned_rows:
            timeframe = row["timeframe"]
            timeframe_ms = TIMEFRAME_MS_BY_INTERVAL[timeframe]
            instrument = row["instrument"]
            candles = row["candles"]
            wf_kwargs = dict(
                **WALK_FORWARD,
                starting_balance=STARTING_BALANCE,
                instrument=instrument,
                expected_candle_ms=timeframe_ms,
                max_missing_bars=0,
            )
            if row["market_type"] == "futures":
                wf_kwargs.update(
                    leverage=2.0,
                    funding_rate=settings.paper_futures_default_funding_rate,
                    funding_interval_hours=settings.paper_futures_funding_interval_hours,
                    maintenance_margin_rate=settings.paper_futures_maintenance_margin_rate,
                    liquidation_fee_rate=settings.paper_futures_liquidation_fee_rate,
                )

            scenarios = [
                (threshold, "baseline") for threshold in VOLUME_THRESHOLDS
            ] + [
                (1.0, ablation) for ablation in ABLATIONS if ablation != "baseline"
            ]
            try:
                for threshold, ablation in scenarios:
                    evaluated = run_walk_forward_backtest(
                        candles,
                        volume_ratio_threshold=threshold,
                        feature_ablation=ablation,
                        **wf_kwargs,
                    )
                    result_row = {
                        "dataset_id": row["id"],
                        "comparison_group_id": group_id,
                        "timeframe": timeframe,
                        "common_window_start_utc": alignment["common_window_start_utc"],
                        "common_window_end_utc": alignment["common_window_end_utc"],
                        "volume_ratio_threshold": threshold,
                        "feature_ablation": ablation,
                        "metrics": _summarize(evaluated, candles),
                    }
                    if threshold == 1.0 and ablation == "baseline":
                        advisor_backtest_config = {
                            "walk_forward": WALK_FORWARD,
                            "starting_balance": STARTING_BALANCE,
                            "fee_rate_per_side": float(settings.taker_fee_rate),
                            "slippage_bps_per_side": float(settings.slippage_bps),
                            "risk_per_trade_pct": float(settings.risk_per_trade_pct),
                            "max_open_risk_pct": float(settings.max_open_risk_pct),
                            "max_daily_loss_pct": float(settings.max_daily_loss_pct),
                            "max_position_notional_pct": float(settings.max_position_notional_pct),
                            "reward_risk_ratio": float(settings.reward_risk_ratio),
                            "volume_ratio_threshold": float(settings.signal_min_volume_ratio),
                            "futures_leverage": wf_kwargs.get("leverage", 1.0),
                            "futures_funding_rate": wf_kwargs.get("funding_rate"),
                            "futures_funding_interval_hours": wf_kwargs.get("funding_interval_hours"),
                            "futures_maintenance_margin_rate": wf_kwargs.get("maintenance_margin_rate"),
                            "futures_liquidation_fee_rate": wf_kwargs.get("liquidation_fee_rate"),
                            "instrument_contract_type": instrument.get("contract_type"),
                            "instrument_contract_size": instrument.get("contract_size"),
                        }
                        result_row["advisor_model_approval"] = _build_fold_isolated_advisor_model(
                            evaluated,
                            row,
                            candles,
                            timeframe_ms,
                            advisor_backtest_config,
                        )
                    group_results.append(result_row)
                group_dataset_reports.append(
                    _dataset_report_row(row, alignment["status"], "COMPLETE", None)
                )
                print(
                    f"PASS {row['id']}: {len(candles)} candles in common UTC window, "
                    f"{len(scenarios)} pre-registered evaluations"
                )
            except Exception as error:
                group_error = f"{row['id']}: {type(error).__name__}: {error}"
                errors.append({
                    "dataset_id": row["id"],
                    "comparison_group_id": group_id,
                    "error": group_error,
                })
                break

        if group_error:
            alignment["status"] = "REJECTED_EVALUATION_FAILURE"
            alignment["reason"] = group_error
            comparison_groups.append(alignment)
            for row in aligned_rows:
                dataset_reports.append(
                    _dataset_report_row(row, alignment["status"], "FAILED_EVALUATION", group_error)
                )
            print(f"REJECTED {group_id}: {group_error}")
            continue

        comparison_groups.append(alignment)
        dataset_reports.extend(group_dataset_reports)
        results.extend(group_results)

    baseline = [
        row for row in results
        if row["volume_ratio_threshold"] == 1.0
        and row["feature_ablation"] == "baseline"
    ]
    completed_datasets = [
        row for row in dataset_reports if row["evaluation_status"] == "COMPLETE"
    ]
    total_trades = sum(row["metrics"]["total_closed_trades"] for row in baseline)
    total_folds = sum(row["metrics"]["fold_count"] for row in baseline)
    baseline_folds = [
        fold for row in baseline for fold in row["metrics"]["test_folds"]
    ]
    fold_returns = [row["net_return_pct"] for row in baseline_folds]
    buy_hold = [row["buy_hold_return_pct"] for row in baseline_folds]
    mean_return = statistics.fmean(fold_returns) if fold_returns else 0.0
    mean_benchmark = statistics.fmean(buy_hold) if buy_hold else 0.0
    positive_fold_pct = (
        sum(value > 0 for value in fold_returns) / len(fold_returns) * 100.0
        if fold_returns else 0.0
    )
    total_wins = sum(row["metrics"]["winning_trades"] for row in baseline)
    total_losses = sum(row["metrics"]["losing_trades"] for row in baseline)
    pf_values = [row["metrics"]["profit_factor"] for row in baseline]
    valid_pf = [
        float(value) for value in pf_values
        if value is not None and math.isfinite(float(value))
    ]
    pooled_pf = statistics.fmean(valid_pf) if valid_pf else None

    if len(completed_datasets) < 4 or total_folds < 12 or total_trades < 100:
        evidence = "INCONCLUSIVE_INSUFFICIENT_COVERAGE_OR_TRADES"
    elif mean_return > 0 and positive_fold_pct >= 55 and mean_return > mean_benchmark:
        evidence = "POSITIVE_OUT_OF_SAMPLE_SIGNAL_REQUIRES_INDEPENDENT_REVIEW"
    else:
        evidence = "NO_ROBUST_POSITIVE_EDGE_DEMONSTRATED"

    advisor_diagnostics = _build_advisor_diagnostics(dataset_reports, results)
    report = {
        "report_version": 2,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "code_sha": os.getenv("GITHUB_SHA", "local"),
        "timeframes": list(timeframes),
        "exchanges_requested": list(exchanges),
        "pre_registered_universe": {
            "spot_symbols": list(SPOT_SYMBOLS),
            "futures_symbols": list(FUTURES_SYMBOLS),
            "timeframes": list(timeframes),
            "thresholds": list(VOLUME_THRESHOLDS),
            "feature_ablations": list(ABLATIONS),
        },
        "comparison_protocol": {
            "grouping": "exchange x requested symbol x market type",
            "common_window": "intersection of available UTC coverage, rounded inward to shared timeframe boundaries",
            "minimum_candles_per_timeframe": MINIMUM_CANDLES,
            "missing_timeframes": "reject entire comparison group",
            "missing_bars": "reject entire comparison group; no interpolation or forward filling",
            "forming_candles": "excluded before computing the common window",
        },
        "assumptions": {
            "strategy_parameters": "frozen from backend Settings; no fitting or optimization",
            "execution": "closed-candle signal, next-open entry, fees and slippage applied by the shared backtest engine",
            "same_bar_stop_target": "stop first",
            "missing_bars": "rejected; no interpolation",
            "futures": "2x isolated paper simulation with fixed settings-based funding, maintenance margin and liquidation fee assumptions",
            "buy_hold_benchmark": "long underlying from test-window next open to final close, less estimated round-trip taker fees and slippage",
            "walk_forward": WALK_FORWARD,
            "lookahead_controls": "warm-up-only history, disjoint chronological test folds, purge >= max holding horizon, embargo >= feature lookback",
            "interpretation": "historical research, not a forecast, guarantee, or proof of future profitability",
        },
        "coverage": {
            "comparison_groups_requested": len(comparison_groups),
            "comparison_groups_eligible": sum(
                group["status"] in ("ALIGNED_COMMON_UTC", "SINGLE_TIMEFRAME_NO_CROSS_TIMEFRAME_COMPARISON")
                for group in comparison_groups
            ),
            "comparison_groups_rejected": sum(
                group["status"] not in ("ALIGNED_COMMON_UTC", "SINGLE_TIMEFRAME_NO_CROSS_TIMEFRAME_COMPARISON")
                for group in comparison_groups
            ),
            "datasets_completed": len(completed_datasets),
            "datasets_failed_or_incomplete": len(dataset_reports) - len(completed_datasets),
            "total_baseline_oos_test_folds": total_folds,
            "total_baseline_oos_closed_trades": total_trades,
            "baseline_mean_fold_return_pct": mean_return,
            "baseline_mean_buy_hold_return_pct": mean_benchmark,
            "baseline_mean_excess_vs_buy_hold_pct": mean_return - mean_benchmark,
            "baseline_positive_fold_pct": positive_fold_pct,
            "positive_winning_trade_count": total_wins,
            "losing_trade_count": total_losses,
            "mean_dataset_profit_factor": pooled_pf,
            "evidence_status": evidence,
        },
        "comparison_groups": comparison_groups,
        "datasets": dataset_reports,
        "evaluations": results,
        "advisor_diagnostics": advisor_diagnostics,
        "errors": errors,
    }

    out_dir = Path(os.getenv("STRATEGY_VALIDATION_OUT_DIR", "artifacts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "strategy-validation.json"
    md_path = out_dir / "strategy-validation.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Strategy validation and Advisor diagnostics report",
        "",
        f"- Created (UTC): {report['created_at_utc']}",
        f"- Code SHA: {report['code_sha']}",
        f"- Timeframes: {', '.join(timeframes)}",
        f"- Comparable groups eligible: {report['coverage']['comparison_groups_eligible']}",
        f"- Comparable groups rejected: {report['coverage']['comparison_groups_rejected']}",
        f"- Completed datasets: {len(completed_datasets)}",
        f"- Incomplete datasets: {len(dataset_reports) - len(completed_datasets)}",
        f"- Baseline OOS folds: {total_folds}",
        f"- Baseline OOS trades: {total_trades}",
        f"- Mean test-fold strategy return: {mean_return:.4f}%",
        f"- Mean test-fold buy-and-hold return: {mean_benchmark:.4f}%",
        f"- Mean excess vs buy-and-hold: {mean_return - mean_benchmark:.4f}%",
        f"- Positive test folds: {positive_fold_pct:.1f}%",
        f"- Evidence status: **{evidence}**",
        "",
        "## Common UTC comparison groups",
        "",
        "| Group | Status | UTC start | UTC end | Duration (days) | Reason |",
        "|---|---|---|---|---:|---|",
    ]
    for group in comparison_groups:
        lines.append(
            f"| {group['comparison_group_id']} | {group['status']} | "
            f"{group.get('common_window_start_utc') or 'N/A'} | "
            f"{group.get('common_window_end_utc') or 'N/A'} | "
            f"{_format_metric(group.get('common_duration_days'), 2)} | "
            f"{(group.get('reason') or '').replace('|', '/')} |"
        )

    lines.extend([
        "",
        "## Strategy dataset results",
        "",
        "| Dataset | Threshold | Feature ablation | OOS trades | Mean return | Mean excess | Expectancy (R) | Profit factor | Positive folds |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in results:
        metrics = row["metrics"]
        lines.append(
            f"| {row['dataset_id']} | {row['volume_ratio_threshold']:.1f} | "
            f"{row['feature_ablation']} | {metrics['total_closed_trades']} | "
            f"{metrics['mean_fold_net_return_pct']:.4f}% | "
            f"{metrics['mean_excess_vs_buy_hold_pct']:.4f}% | "
            f"{metrics['expectancy_r']:.4f} | {metrics['profit_factor']} | "
            f"{metrics['positive_test_fold_pct']:.1f}% |"
        )

    lines.extend([
        "",
        "## Advisor diagnostics by dataset",
        "",
        "| Dataset | UTC start | UTC end | Candles | WF folds | OOS signals | Eligible samples | Excluded samples | Mean R | Top tercile Mean R | Uplift | Uplift 95% CI | Wilson CI positive R | Model status |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ])
    for row in advisor_diagnostics:
        lines.append(
            f"| {row['dataset_id']} | {row.get('common_window_start_utc') or 'N/A'} | "
            f"{row.get('common_window_end_utc') or 'N/A'} | {row.get('candle_count') or 0} | "
            f"{row['walk_forward_folds']} | {row['oos_signals']} | "
            f"{row['eligible_samples']} | {row['excluded_samples']} | "
            f"{_format_metric(row['mean_r'])} | "
            f"{_format_metric(row['top_tercile_mean_r'])} | "
            f"{_format_metric(row['uplift'])} | "
            f"{_format_interval(row['uplift_confidence_interval_95'])} | "
            f"{_format_interval(row['wilson_ci_positive_r_share_95'])} | "
            f"{row['model_status']} |"
        )

    lines.extend([
        "",
        "### Excluded sample reasons",
        "",
    ])
    for row in advisor_diagnostics:
        if row["excluded_sample_reasons"]:
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in row["excluded_sample_reasons"].items()
            )
            lines.append(f"- {row['dataset_id']}: {reasons}")
    if not any(row["excluded_sample_reasons"] for row in advisor_diagnostics):
        lines.append("- No excluded OOS exits were observed in the completed baseline folds.")

    lines.extend([
        "",
        "### Advisor-model interpretation",
        "",
        "- Eligible samples are eligible closed trades in chronological OOS test folds; they are not model-training rows.",
        "- Eligible exits are STOP, STOP_GAP, TARGET, TARGET_GAP and TIME_EXIT. PERIOD_END and all LIQUIDATION exits are excluded.",
        "- Wilson CI is the interval for the positive-R share across all eligible OOS samples; it is not an uplift interval.",
        "- The R-multiple ranker is trained independently inside each expanding Walk-Forward fold using only earlier Train+Validation samples; the following Test fold is never used for that fold's fit.",
        "- A group is approved only when at least three folds each have >=200 eligible training examples and >=20 eligible Test outcomes, with >=100 OOS signals and >=100 eligible OOS outcomes overall.",
        "- Top-tercile Uplift must be positive and the lower bound of the deterministic 95% circular moving-block bootstrap interval must be strictly above zero.",
        "- Approval is per exchange × symbol × market type × timeframe group; no examples are pooled across groups.",
        "- model_status=DISABLED applies to the research R-ranker and does not enable or disable the legacy runtime paper-trade Advisor.",
        "",
        "## Interpretation and limits",
        "",
        "- Every multi-timeframe comparison uses the exact same UTC start/end window within each exchange × symbol × market-type group.",
        "- A group is rejected when a requested timeframe is missing, aligned coverage is below 600 candles, or any candle is missing/irregular inside the common window.",
        "- The test folds are chronological and non-overlapping; every fold resets its own starting balance.",
        "- The fixed strategy is not optimized by this research runner. Threshold and feature-ablation runs are pre-registered sensitivity checks.",
        "- Buy-and-hold is a long-only reference, not a risk-matched portfolio benchmark.",
        "- Spot and Futures are separate datasets; Futures results depend on simplified paper margin/funding/liquidation assumptions.",
        "- Multiple timeframes are sensitivity evidence, not independent samples or a reason to select the best-looking timeframe.",
        "- No result proves a future edge. Positive evidence requires independent review and a later untouched test period.",
        "",
        "## Incomplete datasets and errors",
        "",
    ])
    if errors:
        lines.extend(
            f"- {item.get('dataset_id') or item.get('comparison_group_id')}: {item['error']}"
            for item in errors
        )
    else:
        lines.append("- None.")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"REPORT {json_path}")
    print(f"REPORT {md_path}")
    print(f"EVIDENCE_STATUS {evidence}")
    return 0 if completed_datasets else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
