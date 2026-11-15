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


def test_non_ascii_basic_auth_credentials_return_401_not_500(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    token = base64.b64encode("trãder:sëcret".encode("utf-8")).decode("ascii")
    with TestClient(app) as client:
        response = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


def test_failed_basic_auth_attempts_are_rate_limited_per_cloudflare_client_ip(monkeypatch):
    import backend.main as main

    main._AUTH_FAILURES.clear()
    monkeypatch.setenv("DASHBOARD_USERNAME", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(settings, "auth_rate_limit_max_attempts", 3)
    monkeypatch.setattr(settings, "auth_rate_limit_window_seconds", 300.0)
    bad_token = base64.b64encode(b"wrong:wrong").decode("ascii")
    headers = {
        "Authorization": f"Basic {bad_token}",
        "CF-Ray": "test-ray",
        "CF-Connecting-IP": "198.51.100.22",
    }

    try:
        with TestClient(app) as client:
            first = client.get("/", headers=headers)
            second = client.get("/", headers=headers)
            third = client.get("/", headers=headers)
            fourth = client.get("/", headers=headers)

        assert first.status_code == 401
        assert second.status_code == 401
        assert third.status_code == 429
        assert fourth.status_code == 429
        assert int(third.headers["Retry-After"]) > 0
        assert "198.51.100.22" in main._AUTH_FAILURES
    finally:
        main._AUTH_FAILURES.clear()


def test_auth_limiter_does_not_trust_spoofable_x_forwarded_for():
    import backend.main as main
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", b"203.0.113.99")],
        "client": ("192.0.2.44", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    }
    request = Request(scope)
    assert main._auth_client_ip(request) == "192.0.2.44"
