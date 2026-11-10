import base64

from fastapi.testclient import TestClient

from backend.main import app


def test_health_is_public(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "paper-only"


def test_dashboard_requires_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_dashboard_accepts_valid_basic_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    token = base64.b64encode(b"trader:secret").decode()
    with TestClient(app) as client:
        response = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 200
    assert "AI Crypto Trader" in response.text


def test_api_mutations_require_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    with TestClient(app) as client:
        response = client.post("/api/paper/open", json={
            "exchange": "binance", "symbol": "BTC/USDT", "market_type": "spot",
            "side": "LONG", "entry": 100, "stop": 95
        })
    assert response.status_code == 401


def test_missing_render_credentials_fail_closed(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("DASHBOARD_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 503


def test_missing_oracle_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("DASHBOARD_AUTH_REQUIRED", "true")
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 503
