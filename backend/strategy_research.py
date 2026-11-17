"""Per-group offline strategy acceptance reports.

This module never tunes the live strategy. It evaluates the frozen configuration
on chronological, purged/embargoed walk-forward folds and fails closed.
"""
import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone

from .config import settings
from .data_validation import timeframe_milliseconds
from .indicators import analyze, market_context
from .strategy_approval import current_strategy_config_hash
from .walk_forward import run_walk_forward_backtest


MIN_QUALIFIED_EXAMPLES = 200
MIN_WALK_FORWARD_FOLDS = 3
MIN_OOS_TRADES = 30
BOOTSTRAP_REPLICATES = 2000


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot calculate a percentile from an empty sample")
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_top_tercile_uplift(trades, seed, replicates=BOOTSTRAP_REPLICATES):
    """Circular moving-block bootstrap of top-score-third uplift over all OOS trades."""
    if len(trades) < MIN_OOS_TRADES:
        return {"uplift_r": None, "ci_lower": None, "ci_upper": None}
    rng = random.Random(seed)
    scores = [float(row["signal_score"]) for row in trades]
    outcomes = [float(row["r_multiple"]) for row in trades]
    estimates = []
    block_length = min(5, len(trades))
    for _ in range(replicates):
        indices = []
        while len(indices) < len(trades):
            start = rng.randrange(len(trades))
            indices.extend((start + offset) % len(trades) for offset in range(block_length))
        indices = indices[:len(trades)]
        sample = [(scores[i], outcomes[i]) for i in indices]
        sample.sort(key=lambda item: item[0], reverse=True)
        top_count = max(1, math.ceil(len(sample) / 3))
        all_mean = sum(item[1] for item in sample) / len(sample)
        top_mean = sum(item[1] for item in sample[:top_count]) / top_count
        estimates.append(top_mean - all_mean)
    observed = sorted(zip(scores, outcomes), key=lambda item: item[0], reverse=True)
    top_count = max(1, math.ceil(len(observed) / 3))
    all_mean = sum(outcomes) / len(outcomes)
    top_mean = sum(item[1] for item in observed[:top_count]) / top_count
    return {
        "uplift_r": top_mean - all_mean,
        "ci_lower": _percentile(estimates, 0.025),
        "ci_upper": _percentile(estimates, 0.975),
        "method": "95% percentile circular moving-block bootstrap; 5-trade blocks",
        "replicates": replicates,
        "top_tercile_trade_count": top_count,
    }


def count_qualified_examples(candles, timeframe, higher_timeframe_candles=None,
                             higher_timeframe="4h", require_mtf_confirmation=True,
                             cooldown_bars=1, start_index=199):
    """Count eligible signal candles; adjacent examples may be autocorrelated."""
    interval_ms = timeframe_milliseconds(timeframe)
    higher_interval_ms = None
    higher_end_times = []
    if timeframe == "15m" and require_mtf_confirmation:
        if not isinstance(higher_timeframe_candles, (list, tuple)):
            raise ValueError("15m qualification requires higher-timeframe candles")
        higher_interval_ms = timeframe_milliseconds(higher_timeframe)
        higher_end_times = [
            int(float(row[0])) + higher_interval_ms for row in higher_timeframe_candles
        ]
    qualified = 0
    next_allowed_index = max(199, int(start_index))
    for index in range(next_allowed_index, len(candles)):
        if index < next_allowed_index:
            continue
        metrics = analyze(candles[:index + 1])
        side = metrics.get("signal")
        if side not in ("LONG", "SHORT"):
            continue
        if timeframe == "15m" and require_mtf_confirmation:
            close_ms = int(float(candles[index][0])) + interval_ms
            from bisect import bisect_right
            eligible = bisect_right(higher_end_times, close_ms)
            if eligible < 200:
                continue
            context = market_context(higher_timeframe_candles[:eligible])
            direction = "bullish" if side == "LONG" else "bearish"
            if context["regime"] != "trending" or context["direction"] != direction:
                continue
        qualified += 1
        next_allowed_index = index + max(1, int(cooldown_bars))
    return qualified


def evaluate_strategy_group(*, exchange, symbol, market_type, timeframe, candles,
                            higher_timeframe_candles=None, higher_timeframe="4h",
                            start_utc=None, end_utc=None, instrument=None,
                            train_bars=250, validation_bars=100, test_bars=100,
                            step_bars=100, warmup_bars=200, purge_bars=20,
                            embargo_bars=200, max_holding_bars=20, max_folds=10,
                            min_oos_trades=MIN_OOS_TRADES, bootstrap_replicates=BOOTSTRAP_REPLICATES):
    """Return a reproducible ACCEPT/REJECT report for one exact independent group."""
    if not all(isinstance(value, str) and value.strip() for value in
               (exchange, symbol, market_type, timeframe)):
        raise ValueError("exchange, symbol, market_type and timeframe are required")
    if market_type not in ("spot", "futures"):
        raise ValueError("market_type must be spot or futures")
    interval_ms = timeframe_milliseconds(timeframe)
    if not isinstance(candles, (list, tuple)) or len(candles) < 2:
        raise ValueError("At least two OHLCV candles are required")
    data_hash = hashlib.sha256(
        json.dumps(candles, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    qualified = count_qualified_examples(
        candles, timeframe, higher_timeframe_candles, higher_timeframe,
        timeframe == "15m", start_index=warmup_bars,
    )
    wf = run_walk_forward_backtest(
        candles,
        train_bars=train_bars,
        validation_bars=validation_bars,
        test_bars=test_bars,
        step_bars=step_bars,
        warmup_bars=warmup_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
        max_holding_bars=max_holding_bars,
        max_folds=max_folds,
        instrument=instrument,
        expected_candle_ms=interval_ms,
        max_missing_bars=0,
        candle_timeframe=timeframe,
        higher_timeframe_candles=higher_timeframe_candles,
        higher_timeframe=higher_timeframe,
        require_mtf_confirmation=(timeframe == "15m"),
    )
    trades = [
        row
        for fold in wf["folds"]
        for row in fold["test"].get("oos_trades", [])
    ]
    valid_trades = [
        row for row in trades
        if _finite(row.get("r_multiple")) is not None
        and _finite(row.get("signal_score")) is not None
    ]
    oos_count = len(valid_trades)
    expectancy = (
        sum(float(row["r_multiple"]) for row in valid_trades) / oos_count
        if oos_count else None
    )
    seed_material = f"{exchange}|{symbol}|{market_type}|{timeframe}|{data_hash}"
    seed = int(hashlib.sha256(seed_material.encode()).hexdigest()[:8], 16)
    uplift = _bootstrap_top_tercile_uplift(
        valid_trades, seed, replicates=bootstrap_replicates
    ) if oos_count >= min_oos_trades else {
        "uplift_r": None, "ci_lower": None, "ci_upper": None,
        "reason": f"Need at least {min_oos_trades} OOS trades for uplift CI",
    }
    checks = {
        "qualified_examples": qualified >= MIN_QUALIFIED_EXAMPLES,
        "walk_forward_folds": wf["fold_count"] >= MIN_WALK_FORWARD_FOLDS,
        "oos_trade_count": oos_count >= min_oos_trades,
        "positive_net_expectancy_r": expectancy is not None and expectancy > 0,
        "positive_uplift_ci_lower": (
            uplift.get("ci_lower") is not None and uplift["ci_lower"] > 0
        ),
        "no_open_positions_at_fold_ends": (
            wf["aggregate_test"]["open_positions_at_fold_ends"] == 0
        ),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    report_core = {
        "group": {
            "exchange": exchange, "symbol": symbol,
            "market_type": market_type, "timeframe": timeframe,
        },
        "strategy_version": "3.0.0",
        "config_hash": current_strategy_config_hash(),
        "data_sha256": data_hash,
        "period": {
            "start_utc": start_utc or datetime.fromtimestamp(
                float(candles[0][0]) / 1000, tz=timezone.utc
            ).isoformat(),
            "end_utc": end_utc or datetime.fromtimestamp(
                (float(candles[-1][0]) + interval_ms) / 1000, tz=timezone.utc
            ).isoformat(),
        },
        "data": {"candle_count": max(0, len(candles) - warmup_bars), "warmup_candles": warmup_bars, "interval_ms": interval_ms},
        "qualified_examples": qualified,
        "walk_forward_folds": wf["fold_count"],
        "oos_trade_count": oos_count,
        "net_expectancy_r": expectancy,
        "oos_profit_factor": wf["aggregate_test"]["profit_factor"],
        "worst_fold_max_drawdown_pct": wf["aggregate_test"]["worst_fold_max_drawdown_pct"],
        "uplift": uplift,
        "checks": checks,
        "validation_status": "accepted" if not reasons else "rejected",
        "rejection_reasons": reasons,
        "walk_forward": wf,
    }
    report_id = hashlib.sha256(
        json.dumps(report_core, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]
    report_core["validation_report_id"] = f"wf-{report_id}"
    report_core["generated_at"] = datetime.now(timezone.utc).isoformat()
    return report_core


def build_approval_artifact(reports, strategy_version="3.0.0", validity_days=90):
    """Promote only reports whose complete, exact-group acceptance gate passed."""
    accepted = []
    config_hash = current_strategy_config_hash()
    for report in reports:
        if report.get("validation_status") != "accepted":
            continue
        group = report.get("group", {})
        if report.get("config_hash") != config_hash:
            continue
        accepted.append({
            **group,
            "status": "approved",
            "validation_status": "accepted",
            "validation_report_id": report["validation_report_id"],
            "strategy_version": strategy_version,
            "config_hash": config_hash,
            "qualified_examples": int(report["qualified_examples"]),
            "walk_forward_folds": int(report["walk_forward_folds"]),
            "oos_trade_count": int(report["oos_trade_count"]),
            "net_expectancy_r": float(report["net_expectancy_r"]),
            "uplift_ci_lower": float(report["uplift"]["ci_lower"]),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=validity_days)).isoformat(),
        })
    return {
        "schema_version": 1,
        "strategy_id": "trend-following-ema-di-mtf",
        "strategy_version": strategy_version,
        "status": "approved" if accepted else "research_only",
        "approved_at": datetime.now(timezone.utc).isoformat() if accepted else None,
        "validation_report_id": None,
        "approved_groups": accepted,
        "policy": {
            "minimum_qualified_examples_per_group": MIN_QUALIFIED_EXAMPLES,
            "minimum_valid_walk_forward_folds": MIN_WALK_FORWARD_FOLDS,
            "minimum_oos_trades": MIN_OOS_TRADES,
            "requires_positive_net_expectancy_after_costs": True,
            "requires_positive_top_tercile_uplift_ci_lower": True,
            "paper_trading_only": True,
        },
        "note": "Only exact groups passing the complete offline report are approved; no live orders are enabled.",
    }
