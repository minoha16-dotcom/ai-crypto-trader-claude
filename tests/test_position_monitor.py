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


def test_detailed_monitor_report_counts_rate_limited_position(isolated_db, monkeypatch):
    from backend.exchanges import ExchangeRateLimitCooldown

    opened = open_trade("kucoin", "BTC/USDT", "spot", "LONG", 100, 95)

    async def rate_limited_ticker(exchange, symbol, market_type):
        raise ExchangeRateLimitCooldown(exchange, 429, 60)

    monkeypatch.setattr(registry, "ticker", rate_limited_ticker)
    report = asyncio.run(monitor_open_positions_once(detailed=True))

    assert report == {
        "checked": 1,
        "closed": 0,
        "failed": 1,
        "stale_prices": 0,
        "timestamp_unavailable": 0,
        "remaining_open": 1,
        "failures": [{
            "trade_id": opened["id"],
            "exchange": "kucoin",
            "symbol": "BTC/USDT",
            "market_type": "spot",
            "error_type": "ExchangeRateLimitCooldown",
            "message": "kucoin HTTP 429 rate-limit cooldown is active; retry after 60s",
            "stale_price": False,
            "upstream_status": 429,
            "retry_after_seconds": 60,
        }],
    }

    with connect() as db:
        row = db.execute(
            "SELECT status FROM trades WHERE id=?", (opened["id"],)
        ).fetchone()
    assert row["status"] == "OPEN"



def test_monitor_rejects_stale_timestamped_prices_and_keeps_position_open(
    isolated_db, monkeypatch
):
    import time
    from backend import position_monitor

    opened = open_trade("kucoin", "BTC/USDT", "spot", "LONG", 100, 95)
    now_seconds = 2_000_000_000.0
    monkeypatch.setattr(
        settings, "position_monitor_max_price_age_seconds", 60.0
    )
    monkeypatch.setattr(position_monitor.time, "time", lambda: now_seconds)

    async def stale_ticker(exchange, symbol, market_type):
        return {
            "last": 94.5,
            "timestamp": (now_seconds - 600.0) * 1000.0,
        }

    monkeypatch.setattr(registry, "ticker", stale_ticker)
    report = asyncio.run(monitor_open_positions_once(detailed=True))

    assert report["checked"] == 1
    assert report["closed"] == 0
    assert report["failed"] == 1
    assert report["stale_prices"] == 1
    assert report["remaining_open"] == 1
    assert report["failures"][0]["trade_id"] == opened["id"]
    assert report["failures"][0]["error_type"] == "StaleMarketPrice"
    assert report["failures"][0]["stale_price"] is True
    assert "stale" in report["failures"][0]["message"].lower()

    with connect() as db:
        row = db.execute(
            "SELECT status FROM trades WHERE id=?", (opened["id"],)
        ).fetchone()
    assert row["status"] == "OPEN"


def test_monitor_reports_missing_exchange_timestamp_as_unverified(
    isolated_db, monkeypatch
):
    open_trade("kucoin", "BTC/USDT", "spot", "LONG", 100, 95)

    async def timestamp_missing_ticker(exchange, symbol, market_type):
        return {"last": 100.0, "timestamp": None}

    monkeypatch.setattr(registry, "ticker", timestamp_missing_ticker)
    report = asyncio.run(monitor_open_positions_once(detailed=True))

    assert report["checked"] == 1
    assert report["failed"] == 0
    assert report["timestamp_unavailable"] == 1
    assert report["failures"] == []



def test_monitor_accrues_due_funding_and_liquidates_before_stop_target(
    isolated_db, monkeypatch
):
    from backend import position_monitor
    from backend.paper import open_trade

    instrument = {
        "contract_size": 0.001,
        "contract_type": "linear",
        "amount_precision": 3,
        "precision_mode": 2,
        "min_amount": 0.001,
        "max_amount": 100000,
        "market_type": "futures",
    }
    opened = open_trade(
        "bybit", "BTC/USDT:USDT", "futures", "LONG", 100, 95, 120,
        instrument=instrument, leverage=5, funding_rate=0.001,
        funding_interval_hours=8,
    )
    now_seconds = float(opened["next_funding_at"]) + 1.0
    monkeypatch.setattr(position_monitor.time, "time", lambda: now_seconds)

    async def liquidation_ticker(exchange, symbol, market_type):
        return {"last": 50.0, "timestamp": now_seconds * 1000.0}

    monkeypatch.setattr(registry, "ticker", liquidation_ticker)
    report = asyncio.run(monitor_open_positions_once(detailed=True))

    assert report["checked"] == 1
    assert report["closed"] == 1
    assert report["failed"] == 0

    with connect() as db:
        row = db.execute(
            "SELECT status, exit_reason, funding_paid, funding_events, exit_price, pnl "
            "FROM trades WHERE id=?",
            (opened["id"],),
        ).fetchone()
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "LIQUIDATION"
    assert row["funding_events"] == 1
    assert row["funding_paid"] > 0
    assert row["exit_price"] == pytest.approx(50.0)
    assert row["pnl"] < 0
