import math

import pytest

from backend.advisor_model import (
    FEATURE_NAMES,
    MODEL_VERSION,
    features_from_signal,
    fit_ridge,
    fit_ridge_on_targets,
    predict_r_multiple,
)


def signal(side="LONG", price=105.0, ema50=100.0):
    return {
        "side": side,
        "signal_features": {
            "price": price,
            "ema20": 103.0,
            "ema50": ema50,
            "rsi14": 65.0,
            "adx14": 30.0,
            "volume_ratio": 1.5,
            "atr_pct": 1.2,
        },
    }


def test_feature_vector_collapses_correlated_ema_alignment_and_price_trend():
    features = features_from_signal(signal())
    assert len(features) == len(FEATURE_NAMES) == 5
    assert features[0] > 0
    assert features[1] == pytest.approx(0.3)
    assert all(math.isfinite(value) for value in features)


def test_feature_vector_is_direction_adjusted_for_short_signals():
    long_features = features_from_signal(signal("LONG"))
    short_features = features_from_signal(signal("SHORT"))
    assert long_features[0] == pytest.approx(-short_features[0])
    assert long_features[1] == pytest.approx(-short_features[1])
    assert long_features[2:] == pytest.approx(short_features[2:])


def test_ridge_model_trains_only_on_eligible_net_r_labels():
    rows = []
    for index in range(60):
        x = -1.0 + 2.0 * index / 59
        rows.append({
            "features": [x, 0.2, 0.4, 0.5, 0.3],
            "r_multiple": 2.0 * x,
            "exit_reason": "TARGET",
        })
    rows.append({
        "features": [0, 0, 0, 0, 0],
        "r_multiple": 999,
        "exit_reason": "PERIOD_END",
    })
    model = fit_ridge(rows)
    assert model["model_version"] == MODEL_VERSION
    assert model["training_samples"] == 60
    low = predict_r_multiple(model, {"features": [-0.8, 0.2, 0.4, 0.5, 0.3]})
    high = predict_r_multiple(model, {"features": [0.8, 0.2, 0.4, 0.5, 0.3]})
    assert high > low


def test_ridge_model_rejects_liquidation_training_rows():
    with pytest.raises(ValueError, match="no eligible training rows"):
        fit_ridge([{
            "features": [0, 0, 0, 0, 0],
            "r_multiple": -1,
            "exit_reason": "LIQUIDATION",
        }])


def test_external_expert_pnl_ratio_fit_keeps_target_name_separate_from_r():
    rows = [
        {"features": [index / 20, 0.1, 0.2, 0.3, 0.4], "pnl_ratio": index / 100}
        for index in range(20)
    ]
    model = fit_ridge_on_targets(
        rows,
        target_key="pnl_ratio",
        target_name="leader_pnl_ratio_not_r",
        model_version="okx-expert-roi-ridge-1.0.0",
    )
    assert model["target"] == "leader_pnl_ratio_not_r"
    assert model["training_samples"] == 20
    assert model["label_source"] == "public_okx_copy_trading_history"
