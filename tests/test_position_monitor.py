import asyncio

import pytest

from backend.config import settings
from backend.database import connect, init_db
from backend.exchanges import registry
from backend.paper import open_trade
from backend.position_monitor import monitor_open_positions_once


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'monitor.db'}")
    init_db()


def test_server_monitor_closes_long_when_stop_is_crossed(isolated_db, monkeypatch):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95, 110)

    async def fake_ticker(exchange, symbol, market_type):
        return {"last": 94.5}

    monkeypatch.setattr(registry, "ticker", fake_ticker)
    assert asyncio.run(monitor_open_positions_once()) == 1

    with connect() as db:
        row = db.execute("SELECT status, exit_price FROM trades WHERE id=?", (opened["id"],)).fetchone()
    assert row["status"] == "CLOSED"
    assert row["exit_price"] == pytest.approx(94.5)


def test_server_monitor_closes_short_when_target_is_crossed(isolated_db, monkeypatch):
    opened = open_trade("bybit", "BTC/USDT:USDT", "futures", "SHORT", 100, 105, 90, instrument={"contract_size": 0.001, "contract_type": "linear", "amount_precision": 3, "precision_mode": 2, "min_amount": 0.001})

    async def fake_ticker(exchange, symbol, market_type):
        return {"last": 89.5}

    monkeypatch.setattr(registry, "ticker", fake_ticker)
    assert asyncio.run(monitor_open_positions_once()) == 1

    with connect() as db:
        row = db.execute("SELECT status, exit_price FROM trades WHERE id=?", (opened["id"],)).fetchone()
    assert row["status"] == "CLOSED"
    assert row["exit_price"] == pytest.approx(89.5)


@pytest.mark.parametrize("bad_price", [None, 0, -1, float("inf"), float("nan")])
def test_server_monitor_keeps_position_open_when_price_is_invalid(
    isolated_db, monkeypatch, bad_price
):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95, 110)

    async def fake_ticker(exchange, symbol, market_type):
        return {"last": bad_price}

    monkeypatch.setattr(registry, "ticker", fake_ticker)
    assert asyncio.run(monitor_open_positions_once()) == 0

    with connect() as db:
        row = db.execute("SELECT status FROM trades WHERE id=?", (opened["id"],)).fetchone()
    assert row["status"] == "OPEN"



def test_positions_endpoint_alerts_but_does_not_close_trade(isolated_db, monkeypatch):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95, 110)

    async def fake_ticker(exchange, symbol, market_type):
        return {"last": 94.5, "timestamp": 123456789}

    monkeypatch.setattr(registry, "ticker", fake_ticker)

    from backend.main import get_positions

    result = asyncio.run(get_positions())
    position = next(p for p in result["positions"] if p["id"] == opened["id"])

    assert position["alert"] == "STOP_REACHED"
    assert position["status"] == "OPEN"
    assert position["exit_result"] is None

    with connect() as db:
        row = db.execute(
            "SELECT status FROM trades WHERE id=?", (opened["id"],)
        ).fetchone()
    assert row["status"] == "OPEN"
