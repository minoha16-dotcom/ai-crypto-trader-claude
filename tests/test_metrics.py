import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import init_db
from backend.exchanges import registry
from backend.main import app
from backend.metrics import position_pnl, position_risk, trading_cost_rate
from backend.paper import open_trade


def test_position_pnl_includes_both_sides_costs():
    result = position_pnl(100, 2, "LONG", 105)
    gross = 10.0
    costs = (100 + 105) * 2 * trading_cost_rate()
    assert result["gross_pnl"] == pytest.approx(gross)
    assert result["estimated_costs"] == pytest.approx(costs)
    assert result["unrealized_pnl"] == pytest.approx(gross - costs)
    assert result["unrealized_pnl_pct"] == pytest.approx((gross - costs) / 200 * 100)


def test_short_position_pnl_has_correct_direction():
    result = position_pnl(100, 2, "SHORT", 95)
    assert result["gross_pnl"] == pytest.approx(10.0)
    assert result["unrealized_pnl"] < result["gross_pnl"]


def test_position_risk_matches_entry_to_stop_cost_model():
    expected = (abs(100 - 95) + (100 + 95) * trading_cost_rate()) * 2
    assert position_risk(100, 95, 2) == pytest.approx(expected)


def test_account_positions_and_risk_api_share_same_net_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'metrics.db'}")
    init_db()
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)

    async def fake_ticker(exchange, symbol, market_type):
        return {"last": 102.0, "timestamp": 123456789}

    monkeypatch.setattr(registry, "ticker", fake_ticker)
    with TestClient(app) as client:
        positions = client.get("/api/positions").json()["positions"]
        account_data = client.get("/api/account").json()
        risk_data = client.get("/api/risk").json()

    position = next(p for p in positions if p["id"] == opened["id"])
    assert position["unrealized_pnl"] == pytest.approx(account_data["unrealized_pnl"])
    assert position["unrealized_pnl"] == pytest.approx(risk_data["unrealized_pnl"])
    assert account_data["equity"] == pytest.approx(risk_data["equity"])
    assert risk_data["open_risk"] == pytest.approx(
        position_risk(100, 95, opened["quantity"])
    )
    assert risk_data["daily_pnl"] == pytest.approx(
        risk_data["unrealized_pnl"] + risk_data["realized_pnl"]
    )
