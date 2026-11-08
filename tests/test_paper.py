import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import init_db
from backend.main import app
from backend.paper import open_trade, close_trade, account
from backend.risk import risk_manager


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'paper.db'}")
    init_db()
    yield


def test_spot_short_is_rejected(isolated_db):
    with pytest.raises(ValueError, match="SHORT.*spot"):
        open_trade("binance", "BTC/USDT", "spot", "SHORT", 100, 105)


def test_open_and_close_spot_long(isolated_db):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95, 110)
    assert opened["status"] == "OPEN"
    closed = close_trade(opened["id"], 105)
    assert closed["status"] == "CLOSED"
    assert closed["pnl"] > 0
    assert account()["realized_pnl"] == pytest.approx(closed["pnl"])


def test_close_trade_cannot_be_repeated(isolated_db):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    close_trade(opened["id"], 101)
    with pytest.raises(ValueError, match="not found"):
        close_trade(opened["id"], 102)


def test_daily_realized_loss_blocks_new_trade(isolated_db, monkeypatch):
    monkeypatch.setattr(risk_manager, "max_daily_loss_pct", 3.0)
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    close_trade(opened["id"], 60)
    with pytest.raises(ValueError, match="Daily loss limit"):
        open_trade("binance", "ETH/USDT", "spot", "LONG", 100, 95)


def test_futures_short_is_allowed_in_paper_mode(isolated_db):
    opened = open_trade("bybit", "BTC/USDT:USDT", "futures", "SHORT", 100, 105)
    assert opened["status"] == "OPEN"


def test_api_rejects_spot_short(isolated_db):
    with TestClient(app) as client:
        response = client.post("/api/paper/open", json={
            "exchange": "binance", "symbol": "BTC/USDT", "market_type": "spot",
            "side": "SHORT", "entry": 100, "stop": 105
        })
    assert response.status_code == 422
