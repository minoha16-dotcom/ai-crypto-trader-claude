import pytest

from backend import indicators


def test_directional_feature_ablations_remove_or_collapse_votes(monkeypatch):
    rows = [
        [index * 3_600_000, 100.0, 101.0, 99.0, 100.5, 100.0]
        for index in range(60)
    ]
    monkeypatch.setattr(
        indicators, "ema",
        lambda values, period: 101.0 if period == 20 else 100.0,
    )
    monkeypatch.setattr(indicators, "rsi", lambda values, period=14: 60.0)
    monkeypatch.setattr(indicators, "atr", lambda candles, period=14: 1.0)
    monkeypatch.setattr(indicators, "adx", lambda candles, period=14: 30.0)

    baseline = indicators.analyze(
        rows, volume_ratio_threshold=0.0, feature_ablation="baseline"
    )
    without_ema = indicators.analyze(
        rows, volume_ratio_threshold=0.0,
        feature_ablation="without_ema_alignment",
    )
    without_price = indicators.analyze(
        rows, volume_ratio_threshold=0.0,
        feature_ablation="without_price_vs_ema20",
    )
    composite = indicators.analyze(
        rows, volume_ratio_threshold=0.0,
        feature_ablation="single_composite_trend",
    )

    # Original engine remains a 3-vote score: +EMA alignment +RSI -price/EMA20.
    assert baseline["score"] == 1
    assert baseline["signal"] == "WAIT"
    assert baseline["directional_votes"] == {
        "ema_alignment": 1, "rsi_zone": 1, "price_vs_ema20": -1,
    }

    assert without_ema["score"] == 0
    assert without_ema["directional_votes"]["ema_alignment"] == 0
    assert without_price["score"] == 2
    assert without_price["signal"] == "LONG"
    assert without_price["directional_votes"]["price_vs_ema20"] == 0

    # A single composite trend vote prevents two correlated trend votes from
    # independently pushing a signal over the entry threshold.
    assert composite["score"] == 1
    assert composite["directional_votes"] == {
        "composite_trend": 0, "rsi_zone": 1,
    }


def test_directional_feature_ablation_rejects_unknown_variants():
    rows = [
        [index, 100.0, 101.0, 99.0, 100.0, 100.0]
        for index in range(60)
    ]
    with pytest.raises(ValueError, match="feature_ablation"):
        indicators.analyze(
            rows, volume_ratio_threshold=0.0, feature_ablation="remove_macd"
        )
