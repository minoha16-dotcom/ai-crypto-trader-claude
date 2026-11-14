import base64
import pytest

from fastapi.testclient import TestClient

from backend.config import Settings, settings
from backend.main import app


def test_health_is_public(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "paper-only"


def test_readiness_check_verifies_database_schema():
    with TestClient(app) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["database"] == "ok"


def test_readiness_check_fails_closed_when_database_is_unavailable(monkeypatch):
    def unavailable_database():
        raise RuntimeError("sensitive connection details must not be returned")

    monkeypatch.setattr("backend.main.connect", unavailable_database)
    with TestClient(app) as client:
        response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
    }


def test_dashboard_requires_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    response = TestClient(app).get("/")
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


@pytest.mark.parametrize("username,password", [
    (None, "secret"),
    ("trader", None),
    ("trader", ""),
])
def test_startup_refuses_missing_or_incomplete_credentials(monkeypatch, username, password):
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")
    if username is None:
        monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_USERNAME", username)
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME and DASHBOARD_PASSWORD"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("username,password", [
    (None, "secret"),
    ("trader", None),
    ("trader", ""),
])
def test_protected_requests_fail_closed_without_both_credentials(monkeypatch, username, password):
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")
    if username is None:
        monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_USERNAME", username)
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    response = TestClient(app).get("/")
    assert response.status_code == 503



def test_dashboard_credentials_load_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DASHBOARD_USERNAME=file-user\nDASHBOARD_PASSWORD=file-password\n",
        encoding="utf-8",
    )

    loaded = Settings(_env_file=env_file)
    assert loaded.dashboard_username == "file-user"
    assert loaded.dashboard_password == "file-password"


def test_dashboard_auth_uses_loaded_dotenv_settings_when_process_env_is_missing(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(settings, "dashboard_username", "dotenv-user")
    monkeypatch.setattr(settings, "dashboard_password", "dotenv-password")
    token = base64.b64encode(b"dotenv-user:dotenv-password").decode()

    with TestClient(app) as client:
        response = client.get("/", headers={"Authorization": f"Basic {token}"})

    assert response.status_code == 200
