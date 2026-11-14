"""Chronological walk-forward evaluation for the existing paper backtest engine.

Each period is simulated independently with a fresh starting balance. Indicator
warm-up candles are supplied as read-only history and are never scored. A
configured maximum holding horizon bounds trade outcomes; remaining positions
are force-closed at the period's final candle close. Purge and embargo intervals
are enforced against the holding and feature-lookback horizons. The strategy
configuration is frozen: this runner does not optimize parameters or claim
profitability.
"""
import math

from .backtest import run_backtest
from .indicators import FEATURE_ABLATIONS


def _integer(name, value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer greater than or equal to {minimum}")
    return value


def _validate_candles(candles, expected_candle_ms=None, max_missing_bars=0):
    if not isinstance(candles, (list, tuple)):
        raise ValueError("candles must be a list of OHLCV rows")
    if len(candles) < 2:
        raise ValueError("At least two candles are required")
    if expected_candle_ms is not None:
        expected_candle_ms = float(expected_candle_ms)
        if not math.isfinite(expected_candle_ms) or expected_candle_ms <= 0:
            raise ValueError("expected_candle_ms must be finite and positive")

    previous_timestamp = None
    for index, candle in enumerate(candles):
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            raise ValueError(f"Candle {index} must contain timestamp and OHLCV values")
        timestamp, open_price, high, low, close, volume = map(float, candle[:6])
        if not all(math.isfinite(value) for value in (
            timestamp, open_price, high, low, close, volume
        )):
            raise ValueError(f"Candle {index} contains a non-finite value")
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"Candle {index} contains an invalid OHLCV value")
        if high < max(open_price, close, low) or low > min(open_price, close):
            raise ValueError(f"Candle {index} has inconsistent high/low values")
        if previous_timestamp is not None:
            delta = timestamp - previous_timestamp
            if delta <= 0:
                raise ValueError("Candle timestamps must be strictly increasing and unique")
            if expected_candle_ms is not None:
                intervals = round(delta / expected_candle_ms)
                if intervals < 1 or not math.isclose(
                    delta / expected_candle_ms, intervals, rel_tol=0, abs_tol=0.01
                ):
                    raise ValueError("Candle timestamps are not aligned to the requested timeframe")
                missing = intervals - 1
                if missing > max_missing_bars:
                    raise ValueError(
                        f"Candle data contains {missing} missing bar(s); "
                        f"configured maximum is {max_missing_bars}"
                    )
        previous_timestamp = timestamp


def _period_summary(result, starting_balance, candle_count):
    trades = result.get("trades", [])
    winners = [trade for trade in trades if float(trade.get("pnl", 0)) > 0]
    losers = [trade for trade in trades if float(trade.get("pnl", 0)) < 0]
    breakevens = [trade for trade in trades if float(trade.get("pnl", 0)) == 0]
    gross_profit = sum(float(trade["pnl"]) for trade in winners)
    gross_loss = abs(sum(float(trade["pnl"]) for trade in losers))
    net_pnl = float(result.get("net_pnl", 0.0))
    return {
        "scored_candles": candle_count,
        "closed_trades": len(trades),
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "breakeven_trades": len(breakevens),
        "period_end_closures": sum(
            1 for trade in trades if trade.get("exit_reason") == "PERIOD_END"
        ),
        "liquidations": sum(
            1 for trade in trades if str(trade.get("exit_reason", "")).startswith("LIQUIDATION")
        ),
        "funding_paid": sum(float(trade.get("funding_paid", 0) or 0) for trade in trades),
        "open_positions_at_end": len(result.get("open_positions", [])),
        "skipped_signals": len(result.get("skipped_signals", [])),
        "starting_balance": float(starting_balance),
        "ending_balance": float(result.get("ending_balance", starting_balance)),
        "ending_equity": float(result.get("ending_equity", starting_balance)),
        "net_pnl": net_pnl,
        "net_return_pct": net_pnl / float(starting_balance) * 100.0,
        "realized_pnl": float(result.get("realized_pnl", 0.0)),
        "unrealized_pnl": float(result.get("unrealized_pnl", 0.0)),
        "win_rate_pct": float(result.get("win_rate_pct", 0.0)),
        "expectancy_r": float(result.get("expectancy_r", 0.0)),
        "profit_factor": result.get("profit_factor"),
        "max_drawdown_pct": float(result.get("max_drawdown_pct", 0.0)),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "volume_ratio_threshold": result.get("assumptions", {}).get(
            "volume_ratio_threshold"
        ),
    }


def _range(candles, start, end):
    """Describe a half-open [start, end) interval in source-candle coordinates."""
    return {
        "start_index": start,
        "end_index_exclusive": end,
        "candle_count": end - start,
        "start_timestamp": candles[start][0] if end > start else None,
        "end_timestamp": candles[end - 1][0] if end > start else None,
    }


def _evaluate_period(
    candles, start, end, warmup_bars, starting_balance,
    instrument, volume_ratio_threshold, feature_ablation, max_holding_bars,
    leverage, funding_rate, funding_interval_hours,
    maintenance_margin_rate, liquidation_fee_rate,
):
    history_start = start - warmup_bars
    period_candles = candles[history_start:end]
    # run_backtest starts scoring at this index; all earlier rows are indicator
    # context only and cannot generate a trade in the evaluated period.
    backtest_options = {
        "starting_balance": starting_balance,
        "instrument": instrument,
        "volume_ratio_threshold": volume_ratio_threshold,
        "start_index": warmup_bars,
        "max_holding_bars": max_holding_bars,
        "close_open_at_end": True,
        "leverage": leverage,
        "funding_rate": funding_rate,
        "funding_interval_hours": funding_interval_hours,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_fee_rate": liquidation_fee_rate,
    }
    if feature_ablation != "baseline":
        backtest_options["feature_ablation"] = feature_ablation
    result = run_backtest(period_candles, **backtest_options)
    return _period_summary(result, starting_balance, end - start)


def run_walk_forward_backtest(
    candles,
    *,
    train_bars=250,
    validation_bars=100,
    test_bars=100,
    step_bars=100,
    warmup_bars=50,
    purge_bars=20,
    embargo_bars=None,
    max_holding_bars=20,
    window_mode="rolling",
    max_folds=10,
    starting_balance=10000.0,
    instrument=None,
    volume_ratio_threshold=None,
    feature_ablation="baseline",
    expected_candle_ms=None,
    max_missing_bars=0,
    leverage=1.0,
    funding_rate=None,
    funding_interval_hours=None,
    maintenance_margin_rate=None,
    liquidation_fee_rate=None,
):
    """Evaluate a fixed strategy in non-overlapping chronological test folds.

    train_bars and validation_bars are nominal window sizes. The engine enforces
    purge_bars >= max_holding_bars and embargo_bars >= max(warmup_bars,
    max_holding_bars). Every period is replayed with warm-up-only history
    immediately before its scored range. Positions are time-exited at the
    configured holding limit and force-closed at the period end, so no trade or
    unrealized mark-to-market P&L carries across a split. Test windows never
    overlap.
    """
    train_bars = _integer("train_bars", train_bars, 3)
    validation_bars = _integer("validation_bars", validation_bars, 3)
    test_bars = _integer("test_bars", test_bars, 2)
    step_bars = _integer("step_bars", step_bars, 1)
    warmup_bars = _integer("warmup_bars", warmup_bars, 50)
    purge_bars = _integer("purge_bars", purge_bars, 0)
    max_holding_bars = _integer("max_holding_bars", max_holding_bars, 1)
    if embargo_bars is None:
        embargo_bars = max(warmup_bars, max_holding_bars)
    else:
        embargo_bars = _integer("embargo_bars", embargo_bars, 0)
    max_folds = _integer("max_folds", max_folds, 1)
    max_missing_bars = _integer("max_missing_bars", max_missing_bars, 0)

    if purge_bars < max_holding_bars:
        raise ValueError(
            "purge_bars must be at least max_holding_bars to bound trade-label overlap"
        )
    required_embargo = max(warmup_bars, max_holding_bars)
    if embargo_bars < required_embargo:
        raise ValueError(
            "embargo_bars must be at least max(warmup_bars, max_holding_bars)"
        )
    if train_bars <= purge_bars + 1:
        raise ValueError("train_bars must leave at least two scored candles after purging")
    if validation_bars <= purge_bars + 1:
        raise ValueError("validation_bars must leave at least two scored candles after purging")
    if step_bars < test_bars:
        raise ValueError("step_bars must be at least test_bars to prevent overlapping test folds")
    if max_folds > 50:
        raise ValueError("max_folds cannot exceed 50")
    if window_mode not in ("rolling", "expanding"):
        raise ValueError("window_mode must be 'rolling' or 'expanding'")
    starting_balance = float(starting_balance)
    if not math.isfinite(starting_balance) or starting_balance <= 0:
        raise ValueError("starting_balance must be finite and positive")
    if volume_ratio_threshold is not None:
        volume_ratio_threshold = float(volume_ratio_threshold)
        if not math.isfinite(volume_ratio_threshold) or volume_ratio_threshold < 0:
            raise ValueError("volume_ratio_threshold must be finite and non-negative")
    if feature_ablation not in FEATURE_ABLATIONS:
        raise ValueError(
            "feature_ablation must be one of: " + ", ".join(FEATURE_ABLATIONS)
        )

    _validate_candles(candles, expected_candle_ms, max_missing_bars)
    candle_count = len(candles)
    folds = []
    offset = 0
    truncated = False

    while True:
        if window_mode == "rolling":
            train_start = warmup_bars + offset
            train_nominal_end = train_start + train_bars
        else:
            train_start = warmup_bars
            train_nominal_end = train_start + train_bars + offset

        train_end = train_nominal_end - purge_bars
        validation_start = train_nominal_end + embargo_bars
        validation_nominal_end = validation_start + validation_bars
        validation_end = validation_nominal_end - purge_bars
        test_start = validation_nominal_end + embargo_bars
        test_end = test_start + test_bars

        if test_end > candle_count:
            break
        if len(folds) >= max_folds:
            truncated = True
            break

        train_result = _evaluate_period(
            candles, train_start, train_end, warmup_bars, starting_balance,
            instrument, volume_ratio_threshold, feature_ablation, max_holding_bars,
            leverage, funding_rate, funding_interval_hours,
            maintenance_margin_rate, liquidation_fee_rate,
        )
        validation_result = _evaluate_period(
            candles, validation_start, validation_end, warmup_bars, starting_balance,
            instrument, volume_ratio_threshold, feature_ablation, max_holding_bars,
            leverage, funding_rate, funding_interval_hours,
            maintenance_margin_rate, liquidation_fee_rate,
        )
        test_result = _evaluate_period(
            candles, test_start, test_end, warmup_bars, starting_balance,
            instrument, volume_ratio_threshold, feature_ablation, max_holding_bars,
            leverage, funding_rate, funding_interval_hours,
            maintenance_margin_rate, liquidation_fee_rate,
        )

        folds.append({
            "fold": len(folds) + 1,
            "ranges": {
                "train": _range(candles, train_start, train_end),
                "purge_after_train": _range(candles, train_end, train_nominal_end),
                "embargo_after_train": _range(
                    candles, train_nominal_end, validation_start
                ),
                "validation": _range(candles, validation_start, validation_end),
                "purge_after_validation": _range(
                    candles, validation_end, validation_nominal_end
                ),
                "embargo_after_validation": _range(
                    candles, validation_nominal_end, test_start
                ),
                "test": _range(candles, test_start, test_end),
            },
            "warmup": {
                "candles_per_period": warmup_bars,
                "scored": False,
            },
            "train": train_result,
            "validation": validation_result,
            "test": test_result,
        })
        offset += step_bars

    if not folds:
        minimum = (
            warmup_bars + train_bars + validation_bars + test_bars
            + 2 * embargo_bars
        )
        raise ValueError(
            f"Insufficient candles for one walk-forward fold: need at least "
            f"{minimum}, received {candle_count}"
        )

    test_periods = [fold["test"] for fold in folds]
    total_trades = sum(period["closed_trades"] for period in test_periods)
    total_wins = sum(period["winning_trades"] for period in test_periods)
    total_losses = sum(period["losing_trades"] for period in test_periods)
    total_breakevens = sum(period["breakeven_trades"] for period in test_periods)
    gross_profit = sum(period["gross_profit"] for period in test_periods)
    gross_loss = sum(period["gross_loss"] for period in test_periods)
    total_r = sum(
        period["expectancy_r"] * period["closed_trades"]
        for period in test_periods
    )
    returns = [period["net_return_pct"] for period in test_periods]

    return {
        "engine": "fixed-strategy chronological walk-forward v1",
        "window_mode": window_mode,
        "fold_count": len(folds),
        "max_folds_reached": truncated,
        "data": {
            "candle_count": candle_count,
            "first_timestamp": candles[0][0],
            "last_timestamp": candles[-1][0],
            "expected_candle_ms": expected_candle_ms,
            "max_missing_bars": max_missing_bars,
        },
        "policy": {
            "train_bars_nominal": train_bars,
            "validation_bars_nominal": validation_bars,
            "test_bars": test_bars,
            "step_bars": step_bars,
            "warmup_bars": warmup_bars,
            "purge_bars": purge_bars,
            "embargo_bars": embargo_bars,
            "max_holding_bars": max_holding_bars,
            "futures_leverage": leverage,
            "futures_funding_rate": funding_rate,
            "futures_funding_interval_hours": funding_interval_hours,
            "futures_maintenance_margin_rate": maintenance_margin_rate,
            "futures_liquidation_fee_rate": liquidation_fee_rate,
            "futures_margin_mode": "isolated",
            "boundary_gaps": {
                "purge_minimum": "max_holding_bars",
                "embargo_minimum": "max(warmup_bars, max_holding_bars)",
            },
            "test_windows_overlap": False,
            "period_balances_reset_independently": True,
            "strategy_parameters": "fixed from backend Settings; no automatic tuning",
            "feature_ablation": feature_ablation,
            "train_validation_role": (
                "diagnostic in-sample and validation measurements; no parameter "
                "selection is performed by this runner"
            ),
            "open_positions": (
                "all remaining positions are force-closed at the final candle "
                "close of each period with normal exit fees and slippage; no "
                "unrealized P&L carries into another period"
            ),
        },
        "folds": folds,
        "aggregate_test": {
            "total_closed_trades": total_trades,
            "winning_trades": total_wins,
            "losing_trades": total_losses,
            "breakeven_trades": total_breakevens,
            "trade_weighted_win_rate_pct": (
                total_wins / total_trades * 100.0 if total_trades else 0.0
            ),
            "mean_fold_net_return_pct": (
                sum(returns) / len(returns) if returns else 0.0
            ),
            "sum_of_independent_fold_net_pnl": sum(
                period["net_pnl"] for period in test_periods
            ),
            "expectancy_r": total_r / total_trades if total_trades else 0.0,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
            "worst_fold_max_drawdown_pct": max(
                period["max_drawdown_pct"] for period in test_periods
            ),
            "open_positions_at_fold_ends": sum(
                period["open_positions_at_end"] for period in test_periods
            ),
        },
        "limitations": [
            "This runner evaluates the current fixed strategy; it does not optimize or select parameters.",
            "Each period starts with an independent balance; fold P&L is not a continuous portfolio equity curve.",
            "The walk-forward runner applies a configured maximum holding period and force-closes remaining positions at each period end; this differs from the unbounded single-window backtest.",
            "Purge and embargo constraints bound the configured feature lookback and trade horizon but are safeguards, not a formal proof against every possible source of leakage.",
            "Futures funding is a fixed assumed rate settled on the first candle at/after each interval; missed events use that candle's open.",
            "Futures use isolated margin with a fixed maintenance-margin rate; exchange tier tables, mark-price feeds, bankruptcy prices, order-book depth and real fills are not modeled.",
            "A strategy is not validated until pre-registered folds, sensitivity checks, and out-of-sample results are independently reviewed.",
        ],
    }
