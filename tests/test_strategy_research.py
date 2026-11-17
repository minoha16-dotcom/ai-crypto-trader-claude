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
