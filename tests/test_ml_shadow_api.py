import base64

from fastapi.testclient import TestClient

from backend.config import settings
from backend.ml_shadow import RAW_FEATURE_NAMES
from backend.main import app


def auth_headers():
    token = base64.b64encode(b"test-trader:test-only-password").decode()
    return {"Authorization": f"Basic {token}"}


def shadow_payload():
    signal = 1_800_000_000_000
    signal = signal - (signal % 900_000) + 899_999
    hour_close = signal - (signal % 3_600_000) + 3_599_999
    if hour_close > signal:
        hour_close -= 3_600_000
    four_hour_close = signal - (signal % 14_400_000) + 14_399_999
    if four_hour_close > signal:
        four_hour_close -= 14_400_000
    raw = {name: 0.0 for name in RAW_FEATURE_NAMES}
    raw.update({"atr_pct": 0.01, "donchian_width_pct": 0.02, "rsi14": 0.5})
    return {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "15m",
        "market_scope": "BINANCE_USDM_PERPETUAL",
        "signal_time_ms": signal, "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": hour_close, "htf_4h_close_time_ms": four_hour_close,
        "raw_features": raw,
    }


def test_shadow_prediction_endpoint_is_disabled_by_default_and_does_not_open_trade(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'shadow.db'}")
    monkeypatch.setattr(settings, "ml_shadow_mode_enabled", False)
    monkeypatch.setattr(settings, "ml_shadow_model_path", "")
    monkeypatch.setattr("backend.main.open_trade", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("trade function called")))
    with TestClient(app) as client:
        status = client.get("/api/ml-shadow/status", headers=auth_headers())
        response = client.post("/api/ml-shadow/predict", json=shadow_payload(), headers=auth_headers())
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["trade_decision_changed"] is False
    assert response.status_code == 409
