import pytest

from scripts import run_strategy_validation


def test_strategy_validation_defaults_to_multiple_timeframes(monkeypatch):
    monkeypatch.delenv("STRATEGY_VALIDATION_TIMEFRAMES", raising=False)
    assert run_strategy_validation._parse_timeframes() == ("1h", "4h")


def test_strategy_validation_accepts_pre_registered_timeframe_matrix(monkeypatch):
    monkeypatch.setenv("STRATEGY_VALIDATION_TIMEFRAMES", "15m,1h,4h,1h")
    assert run_strategy_validation._parse_timeframes() == ("15m", "1h", "4h")


def test_strategy_validation_rejects_unsupported_timeframes(monkeypatch):
    monkeypatch.setenv("STRATEGY_VALIDATION_TIMEFRAMES", "1h,3m")
    with pytest.raises(ValueError, match="unsupported strategy-validation timeframe"):
        run_strategy_validation._parse_timeframes()


def _alignment_dataset(timeframe, start_ms, count, symbol="BTC/USDT"):
    width = run_strategy_validation.TIMEFRAME_MS_BY_INTERVAL[timeframe]
    rows = []
    for index in range(count):
        price = 100.0 + index * 0.01
        rows.append([
            start_ms + index * width,
            price,
            price + 1.0,
            price - 1.0,
            price,
            100.0,
        ])
    return {
        "id": f"kucoin:{timeframe}:spot:{symbol}",
        "comparison_group_id": f"kucoin:spot:{symbol}",
        "exchange": "kucoin",
        "timeframe": timeframe,
        "market_type": "spot",
        "requested_symbol": symbol,
        "candles": rows,
        "instrument": {"symbol": symbol, "contract_type": "spot", "contract_size": 1.0},
        "source_metadata": {},
        "error": None,
    }


def test_common_utc_alignment_trims_1h_and_4h_to_exact_shared_window():
    hour = 60 * 60 * 1000
    hourly = _alignment_dataset("1h", 0, 60)
    four_hour = _alignment_dataset("4h", 4 * hour, 14)

    alignment, aligned = run_strategy_validation._align_group_to_common_utc(
        [hourly, four_hour], ("1h", "4h"), min_candles=10
    )

    assert alignment["status"] == "ALIGNED_COMMON_UTC"
    assert alignment["common_window_start_ms"] == 4 * hour
    assert alignment["common_window_end_ms"] == 60 * hour
    by_timeframe = {row["timeframe"]: row for row in aligned}
    assert len(by_timeframe["1h"]["candles"]) == 56
    assert len(by_timeframe["4h"]["candles"]) == 14
    for row in aligned:
        timeframe_ms = run_strategy_validation.TIMEFRAME_MS_BY_INTERVAL[row["timeframe"]]
        assert row["candles"][0][0] == alignment["common_window_start_ms"]
        assert row["candles"][-1][0] + timeframe_ms == alignment["common_window_end_ms"]


def test_common_utc_alignment_rejects_a_missing_requested_timeframe():
    hourly = _alignment_dataset("1h", 0, 800)

    alignment, aligned = run_strategy_validation._align_group_to_common_utc(
        [hourly], ("1h", "4h"), min_candles=10
    )

    assert aligned == []
    assert alignment["status"] == "REJECTED_MISSING_TIMEFRAME_DATA"
    assert "4h" in alignment["reason"]


def test_common_utc_alignment_rejects_insufficient_overlap_after_trimming():
    hour = 60 * 60 * 1000
    hourly = _alignment_dataset("1h", 0, 24)
    four_hour = _alignment_dataset("4h", 16 * hour, 2)

    alignment, aligned = run_strategy_validation._align_group_to_common_utc(
        [hourly, four_hour], ("1h", "4h"), min_candles=3
    )

    assert aligned == []
    assert alignment["status"] == "REJECTED_INSUFFICIENT_COMMON_COVERAGE"
    assert "minimum required is 3" in alignment["reason"]


def test_common_utc_alignment_rejects_internal_gaps_instead_of_filling():
    hourly = _alignment_dataset("1h", 0, 60)
    hourly["candles"].pop(10)
    four_hour = _alignment_dataset("4h", 0, 15)

    alignment, aligned = run_strategy_validation._align_group_to_common_utc(
        [hourly, four_hour], ("1h", "4h"), min_candles=10
    )

    assert aligned == []
    assert alignment["status"] == "REJECTED_INSUFFICIENT_COMMON_COVERAGE"
    assert "missing candle" in alignment["reason"]


def test_advisor_diagnostics_expose_oos_metrics_and_keep_r_ranker_disabled_without_model():
    dataset = {
        "id": "kucoin:1h:spot:BTC/USDT",
        "comparison_group_id": "kucoin:spot:BTC/USDT",
        "exchange": "kucoin",
        "symbol": "BTC/USDT",
        "market_type": "spot",
        "timeframe": "1h",
        "common_window_start_utc": "2026-01-01T00:00:00+00:00",
        "common_window_end_utc": "2026-02-01T00:00:00+00:00",
        "candle_count": 744,
    }
    results = [{
        "dataset_id": dataset["id"],
        "volume_ratio_threshold": 1.0,
        "feature_ablation": "baseline",
        "metrics": {
            "fold_count": 2,
            "oos_signals": 12,
            "total_closed_trades": 8,
            "eligible_samples": 6,
            "excluded_samples": 2,
            "mean_r": 0.25,
            "positive_r_share": 0.5,
            "wilson_ci_positive_r_share_95": [0.1876, 0.8124],
            "excluded_sample_reasons": {"PERIOD_END": 1, "LIQUIDATION": 1},
        },
    }]

    diagnostic = run_strategy_validation._build_advisor_diagnostics([dataset], results)[0]
    assert diagnostic["eligible_samples"] == 6
    assert diagnostic["excluded_samples"] == 2
    assert diagnostic["excluded_sample_reasons"] == {"PERIOD_END": 1, "LIQUIDATION": 1}
    assert diagnostic["walk_forward_folds"] == 2
    assert diagnostic["oos_signals"] == 12
    assert diagnostic["mean_r"] == 0.25
    assert diagnostic["wilson_ci_positive_r_share_95"] == [0.1876, 0.8124]
    assert diagnostic["top_tercile_mean_r"] is None
    assert diagnostic["uplift"] is None
    assert diagnostic["uplift_confidence_interval_95"] is None
    assert diagnostic["model_status"] == "DISABLED"
