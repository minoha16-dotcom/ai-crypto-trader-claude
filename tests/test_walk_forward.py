import pytest

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
    def run(rows, *, starting_balance, instrument, volume_ratio_threshold, start_index):
        calls.append({
            "rows": rows,
            "starting_balance": starting_balance,
            "instrument": instrument,
            "volume_ratio_threshold": volume_ratio_threshold,
            "start_index": start_index,
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
        candles(400),
        train_bars=100,
        validation_bars=40,
        test_bars=40,
        step_bars=40,
        warmup_bars=50,
        purge_bars=2,
        embargo_bars=1,
        window_mode="rolling",
        max_folds=2,
        starting_balance=1000.0,
        volume_ratio_threshold=0.8,
    )

    assert result["fold_count"] == 2
    assert result["max_folds_reached"] is True
    first, second = result["folds"]
    assert first["ranges"]["train"]["start_index"] == 50
    assert first["ranges"]["train"]["end_index_exclusive"] == 148
    assert first["ranges"]["validation"]["start_index"] == 151
    assert first["ranges"]["validation"]["end_index_exclusive"] == 189
    assert first["ranges"]["test"]["start_index"] == 192
    assert first["ranges"]["test"]["end_index_exclusive"] == 232
    assert second["ranges"]["test"]["start_index"] == 232
    assert second["ranges"]["test"]["end_index_exclusive"] == 272
    assert result["policy"]["test_windows_overlap"] is False
    assert result["aggregate_test"]["total_closed_trades"] == 2
    assert result["aggregate_test"]["trade_weighted_win_rate_pct"] == 100.0

    assert len(calls) == 6
    for call in calls:
        assert call["start_index"] == 50
        assert call["volume_ratio_threshold"] == 0.8
        assert len(call["rows"]) > call["start_index"]
    # The runner passes only historical warm-up plus rows through that period's
    # exclusive end. No candle after a scored range enters its backtest call.
    assert calls[0]["rows"][0][0] == 0
    assert calls[0]["rows"][-1][0] == 147_000
    assert calls[2]["rows"][0][0] == 142_000
    assert calls[2]["rows"][-1][0] == 231_000


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
        purge_bars=2,
        embargo_bars=1,
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
