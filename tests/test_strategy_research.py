import pytest

from backend import strategy_research


def _fake_wf(trade_count=60, folds=3, expectancy=0.4):
    trades = [
        {
            "r_multiple": 1.0 if index < trade_count // 3 else 0.1,
            "signal_score": 90.0 if index < trade_count // 3 else 20.0,
            "entry_timestamp": index,
            "exit_timestamp": index + 1,
            "pnl": 1.0,
            "side": "LONG" if index % 2 == 0 else "SHORT",
            "exit_reason": "STOP" if index % 4 == 0 else "TARGET",
            "bars_held": 5,
        }
        for index in range(trade_count)
    ]
    fold_rows = [
        {"test": {"oos_trades": trades[fold::folds]}}
        for fold in range(folds)
    ]
    return {
        "fold_count": folds,
        "folds": fold_rows,
        "aggregate_test": {
            "profit_factor": 1.8,
            "worst_fold_max_drawdown_pct": 4.0,
            "open_positions_at_fold_ends": 0,
        },
    }


def test_bootstrap_top_tercile_uplift_is_deterministic_and_positive():
    trades = [
        {"signal_score": 90.0 if index < 20 else 10.0,
         "r_multiple": 1.0 if index < 20 else 0.1}
        for index in range(60)
    ]
    first = strategy_research._bootstrap_top_tercile_uplift(
        trades, seed=123, replicates=300
    )
    second = strategy_research._bootstrap_top_tercile_uplift(
        trades, seed=123, replicates=300
    )
    assert first == second
    assert first["uplift_r"] > 0
    assert first["ci_lower"] > 0


def test_report_accepts_only_when_every_group_gate_passes(monkeypatch):
    monkeypatch.setattr(strategy_research, "count_qualified_examples", lambda *a, **k: 250)
    monkeypatch.setattr(
        strategy_research, "run_walk_forward_backtest",
        lambda *a, **k: _fake_wf(),
    )
    candles = [
        [index * 3_600_000, 100, 101, 99, 100, 100]
        for index in range(1400)
    ]
    report = strategy_research.evaluate_strategy_group(
        exchange="kucoin", symbol="BTC/USDT", market_type="spot",
        timeframe="1h", candles=candles, bootstrap_replicates=300,
    )
    assert report["validation_status"] == "accepted"
    assert report["qualified_examples"] == 250
    assert report["walk_forward_folds"] == 3
    assert report["oos_trade_count"] == 60
    assert report["uplift"]["ci_lower"] > 0
    diagnostics = report["trade_diagnostics"]
    assert diagnostics["by_side"]["LONG"]["trades"] == 30
    assert diagnostics["by_side"]["SHORT"]["trades"] == 30
    assert diagnostics["by_exit_reason"]["STOP"]["trades"] == 15
    assert diagnostics["stop_exit_share_pct"] == pytest.approx(25.0)


def test_report_rejects_groups_below_sample_and_fold_thresholds(monkeypatch):
    monkeypatch.setattr(strategy_research, "count_qualified_examples", lambda *a, **k: 199)
    monkeypatch.setattr(
        strategy_research, "run_walk_forward_backtest",
        lambda *a, **k: _fake_wf(trade_count=12, folds=2),
    )
    candles = [
        [index * 3_600_000, 100, 101, 99, 100, 100]
        for index in range(1400)
    ]
    report = strategy_research.evaluate_strategy_group(
        exchange="kucoin", symbol="BTC/USDT", market_type="spot",
        timeframe="1h", candles=candles, bootstrap_replicates=100,
    )
    assert report["validation_status"] == "rejected"
    assert "qualified_examples" in report["rejection_reasons"]
    assert "walk_forward_folds" in report["rejection_reasons"]
    assert "oos_trade_count" in report["rejection_reasons"]


def test_approval_artifact_contains_only_accepted_exact_groups(monkeypatch):
    accepted = {
        "validation_status": "accepted",
        "group": {
            "exchange": "kucoin", "symbol": "BTC/USDT",
            "market_type": "spot", "timeframe": "1h",
        },
        "config_hash": strategy_research.current_strategy_config_hash(),
        "validation_report_id": "wf-accepted",
        "qualified_examples": 250,
        "walk_forward_folds": 3,
        "oos_trade_count": 60,
        "net_expectancy_r": 0.4,
        "uplift": {"ci_lower": 0.1},
    }
    rejected = {**accepted, "validation_status": "rejected",
                "validation_report_id": "wf-rejected"}
    artifact = strategy_research.build_approval_artifact([accepted, rejected])
    assert artifact["status"] == "approved"
    assert len(artifact["approved_groups"]) == 1
    assert artifact["approved_groups"][0]["validation_report_id"] == "wf-accepted"


def test_signal_rejection_diagnostics_group_by_fold_direction_and_reason():
    folds = [
        {"test": {"skipped_signal_diagnostics": [
            {"side": "NEUTRAL", "reason": "REGIME_RANGING", "stage": "signal_eligibility"},
            {"side": "NEUTRAL", "reason": "VOLUME_BELOW_AVERAGE", "stage": "signal_eligibility"},
            {"side": "SHORT", "reason": "MTF_DIRECTION_NOT_CONFIRMED", "stage": "execution_gate"},
        ]}},
        {"test": {"skipped_signal_diagnostics": [
            {"side": "NEUTRAL", "reason": "REGIME_RANGING", "stage": "signal_eligibility"},
        ]}},
    ]

    diagnostics = strategy_research._signal_rejection_diagnostics(folds)

    assert diagnostics["total_rejected_decisions"] == 4
    grouped = {
        (row["direction"], row["reason"], row["stage"]): row
        for row in diagnostics["grouped_by_direction_reason_stage"]
    }
    assert grouped[("NEUTRAL", "REGIME_RANGING", "signal_eligibility")]["count"] == 2
    assert grouped[("NEUTRAL", "REGIME_RANGING", "signal_eligibility")]["fold_counts"] == {
        "1": 1, "2": 1
    }
    assert grouped[("SHORT", "MTF_DIRECTION_NOT_CONFIRMED", "execution_gate")]["count"] == 1



def test_qualified_examples_can_be_counted_per_direction(monkeypatch):
    def alternating_signal(history):
        side = "LONG" if len(history) % 2 == 0 else "SHORT"
        return {"signal": side}

    monkeypatch.setattr(strategy_research, "analyze", alternating_signal)
    candles = [
        [index * 3_600_000, 100, 101, 99, 100, 100]
        for index in range(206)
    ]

    both = strategy_research.count_qualified_examples(
        candles, "1h", start_index=200
    )
    longs = strategy_research.count_qualified_examples(
        candles, "1h", start_index=200, direction_filter="LONG"
    )
    shorts = strategy_research.count_qualified_examples(
        candles, "1h", start_index=200, direction_filter="SHORT"
    )

    assert both == 6
    assert longs == 3
    assert shorts == 3



def test_signal_rule_variant_has_independent_reproducible_hash(monkeypatch):
    monkeypatch.setattr(strategy_research, "count_qualified_examples", lambda *a, **k: 250)
    monkeypatch.setattr(
        strategy_research, "run_walk_forward_backtest",
        lambda *a, **k: _fake_wf(),
    )
    candles = [
        [index * 3_600_000, 100, 101, 99, 100, 100]
        for index in range(1400)
    ]
    baseline = strategy_research.evaluate_strategy_group(
        exchange="hyperliquid", symbol="BTC", market_type="futures",
        timeframe="1h", candles=candles, bootstrap_replicates=100,
        direction_filter="LONG", experiment_name="BASELINE_LONG",
    )
    variant = strategy_research.evaluate_strategy_group(
        exchange="hyperliquid", symbol="BTC", market_type="futures",
        timeframe="1h", candles=candles, bootstrap_replicates=100,
        direction_filter="LONG",
        signal_overrides={"long_require_ema200": False},
        experiment_name="LONG_NO_EMA200",
    )
    assert baseline["base_config_hash"] == variant["base_config_hash"]
    assert baseline["config_hash"] != variant["config_hash"]
    assert variant["experiment"]["signal_overrides"] == {
        "long_require_ema200": False
    }
