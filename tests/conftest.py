import pytest


@pytest.fixture(autouse=True)
def configured_dashboard_credentials(monkeypatch):
    # Keep ordinary API tests explicit about the protected-app configuration.
    monkeypatch.setenv("DASHBOARD_USERNAME", "test-trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-only-password")
