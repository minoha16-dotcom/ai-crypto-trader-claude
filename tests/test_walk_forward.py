import pytest

from backend import backtest as backtest_module
from backend import walk_forward


def candles(count):
    rows = []
    for index in range(count):
        close = 100.0 + index * 0.05
        rows.append([
            index * 1000,
            close - 0.1,
            close + 0.5,
            close - 0.5,
            close,
            100.0,
        ])
    return rows


def fake_backtest(calls):
    def run(
        rows, *, starting_balance, instrument, volume_ratio_threshold, start_index,
        max_holding_bars, close_open_at_end, leverage=1.0, funding_rate=None,
        funding_interval_hours=None, maintenance_margin_rate=None,
        liquidation_fee_rate=None, feature_ablation="baseline",
    ):
        calls.append({
            "rows": rows,
            "starting_balance": starting_balance,
            "instrument": instrument,
            "volume_ratio_threshold": volume_ratio_threshold,
            "feature_ablation": feature_ablation,
            "start_index": start_index,
            "max_holding_bars": max_holding_bars,
            "close_open_at_end": close_open_at_end,
            "leverage": leverage,
            "funding_rate": funding_rate,
            "funding_interval_hours": funding_interval_hours,
            "maintenance_margin_rate": maintenance_margin_rate,
            "liquidation_fee_rate": liquidation_fee_rate,
        })
        pnl = 10.0
        trade = {"pnl": pnl, "r_multiple": 0.5}
        return {
            "trades": [trade],
            "trade_count": 1,
            "open_positions": [],
            "skipped_signals": [],
            "starting_balance": starting_balance,
            "ending_balance": starting_balance + pnl,
            "ending_equity": starting_balance + pnl,
            "realized_pnl": pnl,
            "unrealized_pnl": 0.0,
            "net_pnl": pnl,
            "expectancy_r": 0.5,
            "profit_factor": None,
            "max_drawdown_pct": 1.0,
            "win_rate_pct": 100.0,
            "assumptions": {"volume_ratio_threshold": volume_ratio_threshold},
        }
    return run


def test_rolling_walk_forward_creates_chronological_non_overlapping_test_folds(monkeypatch):
    calls = []
    monkeypatch.setattr(walk_forward, "run_backtest", fake_backtest(calls))
    result = walk_forward.run_walk_forward_backtest(
        candles(420),
        train_bars=100,
        validation_bars=40,
        test_bars=40,
        step_bars=40,
        warmup_bars=50,
        purge_bars=20,
        embargo_bars=50,
        window_mode="rolling",
        max_folds=2,
        starting_balance=1000.0,
        volume_ratio_threshold=0.8,
        feature_ablation="without_ema_alignment",
    )

    assert result["fold_count"] == 2
    assert result["max_folds_reached"] is True
    first, second = result["folds"]
    assert first["ranges"]["train"]["start_index"] == 50
    assert first["ranges"]["train"]["end_index_exclusive"] == 130
    assert first["ranges"]["validation"]["start_index"] == 200
    assert first["ranges"]["validation"]["end_index_exclusive"] == 220
    assert first["ranges"]["test"]["start_index"] == 290
    assert first["ranges"]["test"]["end_index_exclusive"] == 330
    assert second["ranges"]["test"]["start_index"] == 330
    assert second["ranges"]["test"]["end_index_exclusive"] == 370
    assert result["policy"]["test_windows_overlap"] is False
    assert result["aggregate_test"]["total_closed_trades"] == 2
    assert result["aggregate_test"]["trade_weighted_win_rate_pct"] == 100.0

    assert len(calls) == 6
    for call in calls:
        assert call["start_index"] == 50
        assert call["volume_ratio_threshold"] == 0.8
        assert call["feature_ablation"] == "without_ema_alignment"
        assert len(call["rows"]) > call["start_index"]
    # The runner passes only historical warm-up plus rows through that period's
    # exclusive end. No candle after a scored range enters its backtest call.
    assert calls[0]["rows"][0][0] == 0
    assert calls[0]["rows"][-1][0] == 129_000
    assert calls[2]["rows"][0][0] == 240_000
    assert calls[2]["rows"][-1][0] == 329_000
    assert all(call["max_holding_bars"] == 20 for call in calls)
    assert all(call["close_open_at_end"] is True for call in calls)


def test_expanding_mode_keeps_training_start_fixed_and_grows_training_window(monkeypatch):
    calls = []
    monkeypatch.setattr(walk_forward, "run_backtest", fake_backtest(calls))
    result = walk_forward.run_walk_forward_backtest(
        candles(400),
        train_bars=100,
        validation_bars=40,
        test_bars=40,
        step_bars=40,
        warmup_bars=50,
        purge_bars=20,
        embargo_bars=50,
        window_mode="expanding",
        max_folds=2,
    )

    first, second = result["folds"]
    assert first["ranges"]["train"]["start_index"] == 50
    assert second["ranges"]["train"]["start_index"] == 50
    assert second["ranges"]["train"]["candle_count"] > first["ranges"]["train"]["candle_count"]
    assert first["ranges"]["test"]["end_index_exclusive"] <= second["ranges"]["test"]["start_index"]


def test_walk_forward_rejects_timestamp_gaps_beyond_configured_policy():
    rows = candles(600)
    rows[120][0] = rows[119][0] + 3000
    with pytest.raises(ValueError, match="missing bar"):
        walk_forward.run_walk_forward_backtest(
            rows,
            train_bars=100,
            validation_bars=40,
            test_bars=40,
            step_bars=40,
            expected_candle_ms=1000,
            max_missing_bars=0,
        )


def test_walk_forward_rejects_unsorted_or_duplicate_timestamps():
    rows = candles(600)
    rows[100][0] = rows[99][0]
    with pytest.raises(ValueError, match="strictly increasing"):
        walk_forward.run_walk_forward_backtest(rows)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"warmup_bars": 49}, "warmup_bars"),
        ({"step_bars": 20}, "at least test_bars"),
        ({"window_mode": "random"}, "window_mode"),
        ({"train_bars": 2}, "train_bars"),
        ({"validation_bars": 2}, "validation_bars"),
        ({"max_folds": 51}, "max_folds"),
        ({"purge_bars": 19}, "purge_bars must be at least max_holding_bars"),
        ({"embargo_bars": 49}, "embargo_bars must be at least"),
    ],
)
def test_walk_forward_rejects_invalid_configuration(overrides, message):
    with pytest.raises(ValueError, match=message):
        walk_forward.run_walk_forward_backtest(candles(800), **overrides)


def test_walk_forward_requires_at_least_one_complete_fold():
    with pytest.raises(ValueError, match="Insufficient candles for one walk-forward fold"):
        walk_forward.run_walk_forward_backtest(
            candles(200),
            train_bars=100,
            validation_bars=40,
            test_bars=40,
            step_bars=40,
        )


def test_walk_forward_validates_non_finite_volume_threshold():
    with pytest.raises(ValueError, match="volume_ratio_threshold"):
        walk_forward.run_walk_forward_backtest(
            candles(600),
            volume_ratio_threshold=float("nan"),
        )


def test_aggregate_test_counts_breakeven_trades_separately(monkeypatch):
    def mixed_result(
        rows, *, starting_balance, instrument, volume_ratio_threshold,
        start_index, max_holding_bars, close_open_at_end, leverage=1.0,
        funding_rate=None, funding_interval_hours=None,
        maintenance_margin_rate=None, liquidation_fee_rate=None,
    ):
        trades = [
            {"pnl": 10.0, "r_multiple": 1.0},
            {"pnl": -4.0, "r_multiple": -0.4},
            {"pnl": 0.0, "r_multiple": 0.0},
        ]
        return {
            "trades": trades,
            "trade_count": len(trades),
            "open_positions": [],
            "skipped_signals": [],
            "starting_balance": starting_balance,
            "ending_balance": starting_balance + 6.0,
            "ending_equity": starting_balance + 6.0,
            "realized_pnl": 6.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 6.0,
            "expectancy_r": 0.2,
            "profit_factor": 2.5,
            "max_drawdown_pct": 0.5,
            "win_rate_pct": 100.0 / 3.0,
            "assumptions": {"volume_ratio_threshold": volume_ratio_threshold},
        }

    monkeypatch.setattr(walk_forward, "run_backtest", mixed_result)
    result = walk_forward.run_walk_forward_backtest(
        candles(400),
        train_bars=100,
        validation_bars=40,
        test_bars=40,
        step_bars=40,
        warmup_bars=50,
        purge_bars=20,
        embargo_bars=50,
        max_folds=1,
    )

    aggregate = result["aggregate_test"]
    assert aggregate["total_closed_trades"] == 3
    assert aggregate["winning_trades"] == 1
    assert aggregate["losing_trades"] == 1
    assert aggregate["breakeven_trades"] == 1


def test_walk_forward_integration_uses_real_backtest_and_closes_period_boundaries(monkeypatch):
    rows = [
        [index * 1000, 100.0, 101.0, 99.0, 100.0, 100.0]
        for index in range(320)
    ]

    def always_long(_history, volume_ratio_threshold=None):
        return {"signal": "LONG", "price": 100.0, "atr14": 2.0}

    real_run_backtest = backtest_module.run_backtest
    executed = []

    def observed_real_backtest(*args, **kwargs):
        result = real_run_backtest(*args, **kwargs)
        executed.append(result)
        return result

    monkeypatch.setattr(backtest_module, "analyze", always_long)
    # Keep the real backtest implementation in the integration path. Only
    # observe its returned trade details; do not replace it with a fake engine.
    monkeypatch.setattr(walk_forward, "run_backtest", observed_real_backtest)

    result = walk_forward.run_walk_forward_backtest(
        rows,
        train_bars=100,
        validation_bars=40,
        test_bars=30,
        step_bars=30,
        warmup_bars=50,
        purge_bars=3,
        embargo_bars=50,
        max_holding_bars=3,
        max_folds=1,
        starting_balance=10000.0,
    )

    assert len(executed) == 3  # train, validation, and test used real run_backtest
    test_result = result["folds"][0]["test"]
    assert test_result["closed_trades"] > 0
    assert test_result["open_positions_at_end"] == 0
    assert test_result["period_end_closures"] >= 1
    assert test_result["net_pnl"] == pytest.approx(test_result["realized_pnl"])
    assert test_result["unrealized_pnl"] == pytest.approx(0.0)
    assert all(not period["open_positions"] for period in executed)
    reasons = {
        trade["exit_reason"]
        for period in executed
        for trade in period["trades"]
    }
    assert "TIME_EXIT" in reasons
    assert "PERIOD_END" in reasons
    assert result["policy"]["max_holding_bars"] == 3
    assert result["policy"]["boundary_gaps"]["purge_minimum"] == "max_holding_bars"



def test_walk_forward_forwards_isolated_futures_assumptions_to_each_period(monkeypatch):
    calls = []
    monkeypatch.setattr(walk_forward, "run_backtest", fake_backtest(calls))
    result = walk_forward.run_walk_forward_backtest(
        candles(420),
        train_bars=100,
        validation_bars=40,
        test_bars=40,
        step_bars=40,
        warmup_bars=50,
        purge_bars=20,
        embargo_bars=50,
        max_folds=1,
        instrument={
            "contract_type": "linear",
            "contract_size": 0.001,
            "amount_precision": 3,
            "precision_mode": 2,
            "market_type": "futures",
        },
        leverage=3,
        funding_rate=0.0002,
        funding_interval_hours=4,
        maintenance_margin_rate=0.006,
        liquidation_fee_rate=0.004,
    )
    assert result["policy"]["futures_leverage"] == 3
    assert result["policy"]["futures_funding_rate"] == pytest.approx(0.0002)
    assert result["policy"]["futures_funding_interval_hours"] == 4
    assert len(calls) == 3
    assert all(call["leverage"] == 3 for call in calls)
    assert all(call["funding_rate"] == pytest.approx(0.0002) for call in calls)
    assert all(call["funding_interval_hours"] == 4 for call in calls)
