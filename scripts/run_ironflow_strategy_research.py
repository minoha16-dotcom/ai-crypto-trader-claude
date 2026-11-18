"""Offline Walk-Forward research using the verified Ironflow Hyperliquid candles.

No Vike API key or network candle fetch is used. Each symbol/timeframe is a
separate Hyperliquid perpetual-futures research group. This first pass evaluates
the frozen current strategy only; it does not optimize parameters or implement
Donchian/TSMOM alternatives.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.strategy_research import evaluate_strategy_group, build_approval_artifact


SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
TIMEFRAMES = ("1h", "4h", "1d")
EXPECTED_START = "2025-05-25T14:00:00Z"
EXPECTED_END_EXCLUSIVE = "2026-11-01T00:00:00Z"
MIN_FOLDS = 3
WARMUP = 200
TRAIN = 250
VALIDATION = 100
TEST = 100
STEP = 100
MAX_FOLDS = 15


def utc_ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def load_group(path, symbol, timeframe):
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "exchange": "hyperliquid",
        "symbol": symbol,
        "market_type": "futures",
        "timeframe": timeframe,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"{path}: expected {key}={value!r}, got {payload.get(key)!r}")
    candles = payload.get("candles")
    if not isinstance(candles, list) or not candles:
        raise ValueError(f"{path}: candles missing or empty")
    return candles


def required_candles():
    # Mirrors walk_forward.run_walk_forward_backtest's first-fold geometry:
    # warmup + train + validation + test + two embargoes + purges.
    purge = 20
    embargo = WARMUP
    return WARMUP + TRAIN + VALIDATION + TEST + 2 * embargo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="data/ironflow/processed")
    parser.add_argument("--output-dir", default="data/strategy-research/ironflow")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    timeframes = [x.strip() for x in args.timeframes.split(",") if x.strip()]
    coverage_path = input_dir / "coverage-report.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    if coverage.get("overall_status") != "READY_FOR_RESEARCH_VALIDATION":
        raise SystemExit("Coverage report is not READY_FOR_RESEARCH_VALIDATION")
    if coverage.get("requested_start") != EXPECTED_START or coverage.get("requested_end_exclusive") != EXPECTED_END_EXCLUSIVE:
        raise SystemExit("Coverage report interval does not match the verified Ironflow interval")

    reports = []
    for symbol in symbols:
        for timeframe in timeframes:
            group_coverage = coverage.get("groups", {}).get(symbol, {})
            report_base = {
                "group": {"exchange": "hyperliquid", "symbol": symbol,
                          "market_type": "futures", "timeframe": timeframe},
                "strategy_version": "3.1.0-exit-research",
                "strategy_family": "current_frozen_ema_di_strategy",
                "data_source": "Ironflow ironflow/hyperliquid-candles-1h",
                "data_coverage_status": group_coverage.get("status"),
                "requested_start_utc": EXPECTED_START,
                "requested_end_exclusive_utc": EXPECTED_END_EXCLUSIVE,
                "walk_forward_policy": {
                    "warmup_bars": WARMUP, "train_bars": TRAIN,
                    "validation_bars": VALIDATION, "test_bars": TEST,
                    "step_bars": STEP, "max_folds": MAX_FOLDS,
                    "minimum_folds": MIN_FOLDS,
                    "purge_bars": 20, "embargo_bars": WARMUP,
                    "selection": "frozen current strategy; no parameter tuning",
                },
            }
            if group_coverage.get("status") != "STRUCTURALLY_CONTIGUOUS":
                reports.append({**report_base, "validation_status": "rejected",
                                "rejection_reasons": ["DATA_COVERAGE_NOT_COMPLETE"]})
                continue

            candles = load_group(input_dir / f"{symbol}_{timeframe}.json", symbol, timeframe)
            interval_ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(timeframe)
            if interval_ms is None:
                raise ValueError(f"Unsupported timeframe: {timeframe}")
            # Exact timestamp continuity and full requested-window bounds.
            if candles[0][0] != utc_ms(EXPECTED_START):
                raise ValueError(f"{symbol} {timeframe}: unexpected first candle")
            if candles[-1][0] != utc_ms(EXPECTED_END_EXCLUSIVE) - interval_ms:
                raise ValueError(f"{symbol} {timeframe}: unexpected last candle")
            for left, right in zip(candles, candles[1:]):
                if right[0] - left[0] != interval_ms:
                    raise ValueError(f"{symbol} {timeframe}: missing/irregular candle interval")

            # Daily has 524 rows, insufficient for even one valid fold under
            # the registered warmup/train/validation/test/embargo geometry.
            min_rows = required_candles()
            if len(candles) < min_rows:
                reports.append({
                    **report_base,
                    "data": {"candle_count": len(candles), "minimum_for_one_fold": min_rows},
                    "validation_status": "rejected",
                    "rejection_reasons": ["INSUFFICIENT_CANDLES_FOR_WALK_FORWARD_FOLDS"],
                    "walk_forward_folds": 0,
                    "qualified_examples": None,
                    "oos_trade_count": 0,
                    "net_expectancy_r": None,
                })
                continue

            report = evaluate_strategy_group(
                exchange="hyperliquid",
                symbol=symbol,
                market_type="futures",
                timeframe=timeframe,
                candles=candles,
                start_utc=EXPECTED_START,
                end_utc=EXPECTED_END_EXCLUSIVE,
                train_bars=TRAIN,
                validation_bars=VALIDATION,
                test_bars=TEST,
                step_bars=STEP,
                warmup_bars=WARMUP,
                purge_bars=20,
                embargo_bars=WARMUP,
                max_holding_bars=20,
                max_folds=MAX_FOLDS,
                bootstrap_replicates=2000,
                experiment_name="IRONFLOW_BASELINE_CURRENT_FROZEN",
            )
            report["data_source"] = "Ironflow ironflow/hyperliquid-candles-1h"
            report["data_coverage_status"] = group_coverage.get("status")
            report["strategy_family"] = "current_frozen_ema_di_strategy"
            report["walk_forward_policy"]["minimum_folds"] = MIN_FOLDS
            reports.append(report)
            print(
                f"{symbol} {timeframe}: {report['validation_status']} "
                f"qualified={report['qualified_examples']} folds={report['walk_forward_folds']} "
                f"oos={report['oos_trade_count']} expectancy_R={report['net_expectancy_r']} "
                f"reasons={','.join(report['rejection_reasons']) or 'none'}",
                flush=True,
            )

    reports_path = output_dir / "reports.json"
    reports_path.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    approval = build_approval_artifact(reports)
    approval["status"] = "research_only"
    approval["approved_groups"] = []
    approval["note"] = (
        "Hyperliquid Ironflow results are offline research only. This first pass "
        "evaluates the frozen current strategy; alternative strategy families "
        "require separate pre-registered implementations and independent validation."
    )
    (output_dir / "approval-candidate.json").write_text(
        json.dumps(approval, indent=2, default=str), encoding="utf-8"
    )
    summary = {
        "groups": len(reports),
        "accepted": sum(r.get("validation_status") == "accepted" for r in reports),
        "rejected": sum(r.get("validation_status") == "rejected" for r in reports),
        "report_file": str(reports_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
