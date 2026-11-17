import json

import pytest

from backend.config import settings
from backend.indicators import analyze, directional_indicators, market_context
from backend.scanner import apply_higher_timeframe_confirmation
from backend.strategy_approval import approval_for_group


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
            "status": "approved",
        }],
    }), encoding="utf-8")
    assert approval_for_group("kucoin", "BTC/USDT", "spot", "15m", path)[0]
    assert not approval_for_group("kucoin", "ETH/USDT", "spot", "15m", path)[0]
