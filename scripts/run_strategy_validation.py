"""Run a reproducible, pre-registered multi-market walk-forward research report.

This is a paper-only historical evaluation. It fetches public OHLCV data,
excludes the still-forming candle, rejects gaps, and writes JSON + Markdown.
It does not optimize or choose a strategy based on the test results.
"""
import asyncio
import json
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


TIMEFRAME = "1h"
TIMEFRAME_MS = 60 * 60 * 1000
DEFAULT_EXCHANGES = ("kucoin", "okx")
SPOT_SYMBOLS = ("BTC/USDT", "ETH/USDT")
FUTURES_SYMBOLS = ("BTC/USDT",)
VOLUME_THRESHOLDS = (0.8, 1.0, 1.2)
ABLATIONS = (
    "baseline",
    "without_ema_alignment",
    "without_price_vs_ema20",
    "single_composite_trend",
)
LIMIT = 1800
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
    "window_mode": "rolling",
    "max_folds": 10,
}


def _parse_csv(name, default):
    value = os.getenv(name, "")
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    return tuple(items) if items else tuple(default)


def _iso_utc(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


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
        })
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
        "test_folds": test_folds,
    }


async def _fetch_dataset(registry, exchange_id, market_type, requested_symbol, limit):
    spec = await registry.market_spec(exchange_id, requested_symbol, market_type)
    candles, metadata = await registry.ohlcv_with_metadata(
        exchange_id, spec["symbol"], TIMEFRAME, limit, market_type
    )
    # CCXT may include the still-forming candle. Never score it.
    now_ms = int(time.time() * 1000)
    closed = [
        row for row in candles
        if int(row[0]) + TIMEFRAME_MS <= now_ms
    ]
    if len(closed) < MINIMUM_CANDLES:
        raise ValueError(
            f"insufficient closed candles: received={len(closed)}, "
            f"minimum={MINIMUM_CANDLES}, metadata={metadata}"
        )
    return spec, closed, metadata


async def run():
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
    datasets = []
    errors = []
    results = []
    try:
        for exchange_id in exchanges:
            requests = (
                [("spot", symbol) for symbol in SPOT_SYMBOLS]
                + [("futures", symbol) for symbol in FUTURES_SYMBOLS]
            )
            for market_type, requested_symbol in requests:
                key = f"{exchange_id}:{market_type}:{requested_symbol}"
                try:
                    spec, candles, source_meta = await _fetch_dataset(
                        registry, exchange_id, market_type, requested_symbol, limit
                    )
                    # The runner fails closed on any missing bar instead of
                    # interpolating or forward-filling market prices.
                    wf_kwargs = dict(
                        **WALK_FORWARD,
                        starting_balance=STARTING_BALANCE,
                        instrument=spec,
                        expected_candle_ms=TIMEFRAME_MS,
                        max_missing_bars=0,
                    )
                    if market_type == "futures":
                        wf_kwargs.update(
                            leverage=2.0,
                            funding_rate=settings.paper_futures_default_funding_rate,
                            funding_interval_hours=settings.paper_futures_funding_interval_hours,
                            maintenance_margin_rate=settings.paper_futures_maintenance_margin_rate,
                            liquidation_fee_rate=settings.paper_futures_liquidation_fee_rate,
                        )
                    datasets.append({
                        "id": key,
                        "exchange": exchange_id,
                        "market_type": market_type,
                        "symbol": spec["symbol"],
                        "contract_type": spec["contract_type"],
                        "contract_size": spec["contract_size"],
                        "candle_count": len(candles),
                        "first_candle_utc": _iso_utc(candles[0][0]),
                        "last_candle_utc": _iso_utc(candles[-1][0]),
                        "source_metadata": source_meta,
                    })
                    scenarios = [
                        (threshold, "baseline")
                        for threshold in VOLUME_THRESHOLDS
                    ] + [
                        (1.0, ablation)
                        for ablation in ABLATIONS
                        if ablation != "baseline"
                    ]
                    for threshold, ablation in scenarios:
                        evaluated = run_walk_forward_backtest(
                            candles,
                            volume_ratio_threshold=threshold,
                            feature_ablation=ablation,
                            **wf_kwargs,
                        )
                        summary = _summarize(evaluated, candles)
                        results.append({
                            "dataset_id": key,
                            "volume_ratio_threshold": threshold,
                            "feature_ablation": ablation,
                            "metrics": summary,
                        })
                    print(
                        f"PASS {key}: {len(candles)} closed candles, "
                        f"{len(scenarios)} pre-registered evaluations"
                    )
                except Exception as error:
                    errors.append({"dataset_id": key, "error": f"{type(error).__name__}: {error}"})
                    print(f"INCOMPLETE {key}: {type(error).__name__}: {error}")
    finally:
        await registry.close()

    baseline = [
        row for row in results
        if row["volume_ratio_threshold"] == 1.0
        and row["feature_ablation"] == "baseline"
    ]
    total_trades = sum(row["metrics"]["total_closed_trades"] for row in baseline)
    total_folds = sum(row["metrics"]["fold_count"] for row in baseline)
    baseline_folds = [
        fold
        for row in baseline
        for fold in row["metrics"]["test_folds"]
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
    gross_profit = sum(
        max(0.0, row["metrics"]["sum_of_independent_fold_net_pnl"])
        for row in baseline
    )
    pf_values = [row["metrics"]["profit_factor"] for row in baseline]
    valid_pf = [float(value) for value in pf_values if value is not None and math.isfinite(float(value))]
    pooled_pf = statistics.fmean(valid_pf) if valid_pf else None

    if len(datasets) < 4 or total_folds < 12 or total_trades < 100:
        evidence = "INCONCLUSIVE_INSUFFICIENT_COVERAGE_OR_TRADES"
    elif mean_return > 0 and positive_fold_pct >= 55 and mean_return > mean_benchmark:
        evidence = "POSITIVE_OUT_OF_SAMPLE_SIGNAL_REQUIRES_INDEPENDENT_REVIEW"
    else:
        evidence = "NO_ROBUST_POSITIVE_EDGE_DEMONSTRATED"

    report = {
        "report_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "code_sha": os.getenv("GITHUB_SHA", "local"),
        "timeframe": TIMEFRAME,
        "exchanges_requested": list(exchanges),
        "pre_registered_universe": {
            "spot_symbols": list(SPOT_SYMBOLS),
            "futures_symbols": list(FUTURES_SYMBOLS),
            "thresholds": list(VOLUME_THRESHOLDS),
            "feature_ablations": list(ABLATIONS),
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
            "datasets_completed": len(datasets),
            "datasets_failed_or_incomplete": len(errors),
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
        "datasets": datasets,
        "evaluations": results,
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
        "# Strategy validation report",
        "",
        f"- Created (UTC): {report['created_at_utc']}",
        f"- Code SHA: {report['code_sha']}",
        f"- Timeframe: {TIMEFRAME}",
        f"- Completed datasets: {len(datasets)}",
        f"- Incomplete datasets: {len(errors)}",
        f"- Baseline OOS folds: {total_folds}",
        f"- Baseline OOS trades: {total_trades}",
        f"- Mean test-fold strategy return: {mean_return:.4f}%",
        f"- Mean test-fold buy-and-hold return: {mean_benchmark:.4f}%",
        f"- Mean excess vs buy-and-hold: {mean_return - mean_benchmark:.4f}%",
        f"- Positive test folds: {positive_fold_pct:.1f}%",
        f"- Evidence status: **{evidence}**",
        "",
        "## Dataset results",
        "",
        "| Dataset | Threshold | Feature ablation | OOS trades | Mean return | Mean excess | Expectancy (R) | Profit factor | Positive folds |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        m = row["metrics"]
        lines.append(
            f"| {row['dataset_id']} | {row['volume_ratio_threshold']:.1f} | "
            f"{row['feature_ablation']} | {m['total_closed_trades']} | "
            f"{m['mean_fold_net_return_pct']:.4f}% | "
            f"{m['mean_excess_vs_buy_hold_pct']:.4f}% | "
            f"{m['expectancy_r']:.4f} | {m['profit_factor']} | "
            f"{m['positive_test_fold_pct']:.1f}% |"
        )
    lines.extend([
        "",
        "## Interpretation and limits",
        "",
        "- The test folds are chronological and non-overlapping; every fold resets its own starting balance.",
        "- The baseline uses the current fixed backend strategy. Threshold and feature-ablation runs are pre-registered sensitivity checks, not an optimizer.",
        "- The buy-and-hold comparator is long-only and is a reference, not a risk-matched portfolio benchmark.",
        "- Spot and Futures results are separated by dataset. Futures depend on simplified paper margin/funding/liquidation assumptions.",
        "- No result proves a future edge. Positive evidence requires independent review and a later untouched time period.",
        "- Any incomplete dataset, low sample count, or narrow market coverage reduces confidence.",
        "",
        "## Incomplete datasets",
        "",
    ])
    if errors:
        lines.extend(f"- {item['dataset_id']}: {item['error']}" for item in errors)
    else:
        lines.append("- None.")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"REPORT {json_path}")
    print(f"REPORT {md_path}")
    print(f"EVIDENCE_STATUS {evidence}")
    return 0 if datasets else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
