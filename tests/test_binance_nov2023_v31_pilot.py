from __future__ import annotations

from scripts.binance_nov2023_v31_pilot import (
    BAR_MS, FEATURES, build_features, fit_logistic, label_event, predict,
)


def make_bars(count=100):
    rows = []
    for i in range(count):
        close = 100.0 + i * 0.01
        rows.append({
            "t": 1_700_000_000_000 + i * BAR_MS,
            "o": close, "h": close + 0.2, "l": close - 0.2,
            "c": close, "v": 100.0 + (i % 7),
        })
    return rows


def test_features_are_causal_and_have_fixed_schema():
    bars = make_bars()
    before = build_features(bars, 60)
    bars[90]["c"] *= 1.5
    bars[90]["h"] *= 1.5
    after = build_features(bars, 60)
    assert before == after
    assert tuple(before) == FEATURES


def test_label_fails_closed_when_mark_price_is_missing_during_hold():
    bars = make_bars()
    entry_i = 21
    event = {
        "entry_index": entry_i, "side": "LONG",
        "atr14": 1.0,
    }
    mark = {row["t"]: row for row in bars}
    missing_t = bars[entry_i + 5]["t"]
    del mark[missing_t]
    result = label_event(
        event, bars, mark, {missing_t}, [],
        fee_bps=5.0, slippage_bps=2.0,
    )
    assert result["eligible"] is False
    assert result["exit_reason"] == "DATA_GAP"
    assert result["exclusion"] == "MISSING_MARK_PRICE_DURING_HOLD"


def test_logistic_regression_fits_only_when_both_classes_exist():
    rows = []
    for i in range(60):
        features = {key: (i / 60.0) for key in FEATURES}
        rows.append({"features": features, "label": int(i >= 30)})
    model, report = fit_logistic(rows, iterations=100)
    assert report["status"] == "FIT"
    assert model is not None
    probability = predict(model, rows[-1]["features"])
    assert 0.0 <= probability <= 1.0


def test_logistic_regression_refuses_single_class_training():
    rows = [{"features": {key: 0.1 for key in FEATURES}, "label": 1}
            for _ in range(40)]
    model, report = fit_logistic(rows)
    assert model is None
    assert report["status"] == "NOT_FIT"
