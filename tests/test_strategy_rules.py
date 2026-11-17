import json

import pytest

from backend.config import settings
from backend.indicators import analyze, directional_indicators, market_context
from backend.scanner import apply_higher_timeframe_confirmation
from backend import backtest as backtest_module
from backend.backtest import run_backtest
from backend.strategy_approval import approval_for_group, current_strategy_config_hash


def rising_candles(count=260):
    rows = []
    for i in range(count):
        close = 100 + i * 0.5
        rows.append([i * 900_000, close - 0.2, close + 1, close - 1, close, 1000])
    return rows


def falling_candles(count=260):
    rows = []
    for i in range(count):
        close = 300 - i * 0.5
        rows.append([i * 900_000, close + 0.2, close + 1, close - 1, close, 1000])
    return rows


def ranging_candles(count=260):
    return [
        [i * 900_000, 100, 101, 99, 100, 1000]
        for i in range(count)
    ]


def test_wilder_directional_indicators_expose_both_di():
    values = directional_indicators(rising_candles())
    assert values["adx14"] >= 20
    assert values["plus_di14"] > values["minus_di14"]


def test_direction_uses_di_ema20_ema50_and_ema200():
    long_result = analyze(rising_candles())
    short_result = analyze(falling_candles())
    assert long_result["ema200"] is not None
    assert long_result["direction"] == "bullish"
    assert long_result["signal"] == "LONG"
    assert short_result["direction"] == "bearish"
    assert short_result["signal"] == "SHORT"


def test_ranging_market_is_hard_rejected_not_rescued_by_score():
    result = analyze(ranging_candles())
    assert result["regime"] == "ranging"
    assert result["signal"] == "WAIT"
    assert "REGIME_RANGING" in result["filter_reasons"]
    assert result["score"] == 0


def test_ema200_warmup_cannot_create_directional_signal():
    result = analyze(rising_candles(120))
    assert result["ema200"] is None
    assert result["signal"] == "WAIT"
    assert "EMA200_HISTORY_NOT_READY" in result["filter_reasons"]


@pytest.mark.parametrize("side,direction,expected", [
    ("LONG", "bullish", True),
    ("SHORT", "bearish", True),
    ("LONG", "bearish", False),
    ("SHORT", "bullish", False),
    ("LONG", "neutral", False),
    ("LONG", "bullish", False),
])
def test_mtf_gate_requires_same_trending_direction(side, direction, expected):
    metrics = {"signal": side, "filter_reasons": []}
    context = {
        "regime": "ranging" if direction == "bullish" and not expected and side == "LONG" and direction == "bullish" else "trending",
        "direction": direction,
    }
    if side == "LONG" and direction == "bullish" and expected:
        context["regime"] = "trending"
    if side == "SHORT" and direction == "bearish" and expected:
        context["regime"] = "trending"
    confirmed = apply_higher_timeframe_confirmation(metrics, context)
    assert confirmed is expected
    if not expected:
        assert metrics["signal"] == "WAIT"
        assert "MTF_DIRECTION_NOT_CONFIRMED" in metrics["filter_reasons"]


def test_strategy_approval_fails_closed_for_research_only_artifact(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "strategy_version": "3.0.0",
        "status": "research_only",
        "approved_groups": [],
    }), encoding="utf-8")
    approved, reason, _ = approval_for_group(
        "kucoin", "BTC/USDT", "spot", "15m", path
    )
    assert not approved
    assert reason == "STRATEGY_NOT_APPROVED"


def test_strategy_approval_requires_exact_group_and_report(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "strategy_version": "3.0.0",
        "status": "approved",
        "approved_groups": [{
            "exchange": "kucoin",
            "symbol": "BTC/USDT",
            "market_type": "spot",
            "timeframe": "15m",
            "strategy_version": "3.0.0",
            "validation_report_id": "wf-report-001",
            "config_hash": current_strategy_config_hash(),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "status": "approved",
        }],
    }), encoding="utf-8")
    assert approval_for_group("kucoin", "BTC/USDT", "spot", "15m", path)[0]
    assert not approval_for_group("kucoin", "ETH/USDT", "spot", "15m", path)[0]


def _higher_rising_candles(count=500):
    rows = []
    for index in range(count):
        close = 100.0 + index * 0.5
        rows.append([
            index * 14_400_000, close - 0.2, close + 1, close - 1, close, 1000
        ])
    return rows


def _lower_flat_candles(start_timestamp, count=60):
    return [
        [start_timestamp + index * 900_000, 100, 101, 99, 100, 100]
        for index in range(count)
    ]


def test_15m_backtest_does_not_use_future_4h_candles(monkeypatch):
    calls = {"count": 0}

    def one_long(_history, volume_ratio_threshold=None):
        calls["count"] += 1
        return {"signal": "LONG" if calls["count"] == 1 else "WAIT", "price": 100, "atr14": 2}

    monkeypatch.setattr(backtest_module, "analyze", one_long)
    high = _higher_rising_candles(500)
    # The first 15m decision closes when only 199 higher-timeframe candles
    # have closed. The remaining 301 rows are future data and must not leak in.
    lower_start = 199 * 14_400_000 - 45 * 900_000
    result = run_backtest(
        _lower_flat_candles(lower_start),
        candle_timeframe="15m",
        higher_timeframe_candles=high,
        higher_timeframe="4h",
        max_holding_bars=1,
    )
    assert result["trade_count"] == 0
    assert result["skipped_signals"][0]["reason"] == "MTF_HIGHER_TIMEFRAME_WARMUP_NOT_READY"


def test_15m_backtest_accepts_only_closed_aligned_4h_context(monkeypatch):
    calls = {"count": 0}

    def one_long(_history, volume_ratio_threshold=None):
        calls["count"] += 1
        return {"signal": "LONG" if calls["count"] == 1 else "WAIT", "price": 100, "atr14": 2}

    monkeypatch.setattr(backtest_module, "analyze", one_long)
    high = _higher_rising_candles(500)
    # This decision closes exactly at the 200th 4h candle close.
    lower_start = 200 * 14_400_000 - 45 * 900_000
    result = run_backtest(
        _lower_flat_candles(lower_start),
        candle_timeframe="15m",
        higher_timeframe_candles=high,
        higher_timeframe="4h",
        max_holding_bars=1,
    )
    assert result["trade_count"] == 1
    assert result["trades"][0]["side"] == "LONG"


def test_net_reward_risk_is_a_hard_gate_not_a_score_override(monkeypatch):
    monkeypatch.setattr(settings, "signal_min_net_rr", 10.0)
    result = analyze(rising_candles())
    assert result["net_reward_risk"] < 10.0
    assert result["signal"] == "WAIT"
    assert result["score"] == 0
    assert "NET_REWARD_RISK_BELOW_MINIMUM" in result["filter_reasons"]
