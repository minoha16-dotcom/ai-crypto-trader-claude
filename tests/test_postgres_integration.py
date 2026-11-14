"""PostgreSQL integration tests. Run against a disposable CI database only."""
import os

import pytest

from backend.config import settings
from backend.database import connect, init_db
from backend.paper import close_trade, open_trade


@pytest.fixture
def postgres_db(monkeypatch):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setenv("DATABASE_SSLMODE", "disable")

    import psycopg

    # Start from a legacy trades schema to verify the production migration path.
    with psycopg.connect(url) as raw:
        raw.execute("DROP TABLE IF EXISTS trades CASCADE")
        raw.execute("""
            CREATE TABLE trades (
                id BIGSERIAL PRIMARY KEY,
                opened_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMPTZ,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market_type TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                entry DOUBLE PRECISION NOT NULL,
                stop DOUBLE PRECISION NOT NULL,
                target DOUBLE PRECISION,
                exit_price DOUBLE PRECISION,
                pnl DOUBLE PRECISION DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                signal_id BIGINT,
                notes TEXT DEFAULT ''
            )
        """)
        raw.execute("""
            INSERT INTO trades(
                exchange, symbol, market_type, side, quantity, entry, stop,
                target, pnl, status
            ) VALUES ('binance', 'BTC/USDT:USDT', 'futures', 'LONG', 2, 60000, 59000,
                      62000, 125, 'CLOSED')
        """)

    init_db()
    with connect() as db:
        migrated = db.execute(
            "SELECT contract_size, contract_type FROM trades WHERE symbol=?",
            ("BTC/USDT:USDT",),
        ).fetchone()
        assert migrated["contract_size"] == pytest.approx(1.0)
        assert migrated["contract_type"] == "legacy_base"
        db.execute("TRUNCATE trades, signals, daily_equity_snapshots RESTART IDENTITY")

    yield

    with connect() as db:
        db.execute("TRUNCATE trades, signals, daily_equity_snapshots RESTART IDENTITY")


def test_postgres_schema_migration_and_paper_trade_round_trip(postgres_db):
    # Startup migrations must be safe to run more than once.
    init_db()

    opened = open_trade(
        "binance", "ETH/USDT", "spot", "LONG", 100, 95, target=110
    )
    with connect() as db:
        row = db.execute(
            "SELECT status, contract_type, contract_size FROM trades WHERE id=?",
            (opened["id"],),
        ).fetchone()
    assert row["status"] == "OPEN"
    assert row["contract_type"] == "spot"
    assert row["contract_size"] == pytest.approx(1.0)

    closed = close_trade(opened["id"], 108)
    with connect() as db:
        row = db.execute(
            "SELECT status, exit_price, pnl FROM trades WHERE id=?",
            (opened["id"],),
        ).fetchone()
    assert closed["status"] == "CLOSED"
    assert row["status"] == "CLOSED"
    assert row["exit_price"] == pytest.approx(108)
    assert row["pnl"] == pytest.approx(closed["pnl"])


def test_postgres_futures_contract_metadata_persists(postgres_db):
    opened = open_trade(
        "bybit",
        "BTC/USDT:USDT",
        "futures",
        "SHORT",
        100,
        105,
        target=90,
        instrument={
            "contract_size": 0.001,
            "contract_type": "linear",
            "amount_precision": 3,
            "precision_mode": 2,
            "min_amount": 0.001,
            "max_amount": 100000,
        },
    )
    with connect() as db:
        row = db.execute(
            "SELECT market_type, quantity, contract_size, contract_type, "
            "leverage, initial_margin, maintenance_margin_rate, liquidation_price, "
            "funding_rate, funding_interval_hours, funding_paid, funding_events, "
            "next_funding_at FROM trades WHERE id=?",
            (opened["id"],),
        ).fetchone()
    assert row["market_type"] == "futures"
    assert row["quantity"] == pytest.approx(opened["quantity"])
    assert row["contract_size"] == pytest.approx(0.001)
    assert row["contract_type"] == "linear"
    assert row["leverage"] == pytest.approx(1.0)
    assert row["initial_margin"] > 0
    assert row["maintenance_margin_rate"] == pytest.approx(0.005)
    assert row["liquidation_price"] is not None
    assert row["funding_rate"] == pytest.approx(0.0001)
    assert row["funding_interval_hours"] == pytest.approx(8.0)
    assert row["funding_paid"] == pytest.approx(0.0)
    assert row["funding_events"] == 0
    assert row["next_funding_at"] is not None


def test_postgres_monitor_advisory_lock_has_one_leader(postgres_db):
    from backend.position_monitor import _monitor_leader_lock

    with _monitor_leader_lock() as first:
        assert first is True
        with _monitor_leader_lock() as second:
            assert second is False
