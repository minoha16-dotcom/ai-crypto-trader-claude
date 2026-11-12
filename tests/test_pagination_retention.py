from datetime import datetime, timedelta, timezone

import pytest

from backend.config import settings
from backend.database import archive_expired_signals, connect, init_db
from backend.main import signals
from backend.paper import journal


@pytest.fixture
def history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'history.db'}")
    init_db()
    with connect() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO trades(exchange,symbol,market_type,side,quantity,entry,stop,target,status) "
                "VALUES('kucoin',?,'spot','LONG',1,100,95,110,'CLOSED')", (f"T{i}/USDT",)
            )
        for i in range(4):
            db.execute(
                "INSERT INTO signals(created_at,exchange,symbol,market_type,timeframe,signal,score,price,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ((datetime.now(timezone.utc) - timedelta(days=400 if i == 0 else 2)).strftime("%Y-%m-%d %H:%M:%S"),
                 "kucoin", f"S{i}/USDT", "spot", "1h", "LONG", i, 100, "{}"),
            )
    yield


def test_journal_paginates_trades_and_active_signals(history_db):
    first = journal(limit=2, offset=0)
    second = journal(limit=2, offset=2)
    assert len(first["trades"]) == 2
    assert len(first["signals"]) == 2
    assert len(second["trades"]) == 1
    assert len(second["signals"]) == 2
    assert first["pagination"]["trades_total"] == 3
    assert first["pagination"]["signals_total"] == 4
    assert first["pagination"]["trades_has_more"] is True
    assert second["pagination"]["signals_has_more"] is False


def test_signal_endpoint_paginates_with_bounded_inputs(history_db):
    page = signals(limit=1, offset=1)
    assert len(page["signals"]) == 1
    assert page["pagination"] == {
        "limit": 1, "offset": 1, "total": 4, "has_more": True,
    }
    bounded = signals(limit=5000, offset=-3)
    assert bounded["pagination"]["limit"] == 500
    assert bounded["pagination"]["offset"] == 0


def test_signal_retention_soft_archives_without_deleting(history_db):
    assert archive_expired_signals(retention_days=30) == 1
    with connect() as db:
        stored = db.execute("SELECT COUNT(*) AS total FROM signals").fetchone()["total"]
        archived = db.execute(
            "SELECT COUNT(*) AS total FROM signals WHERE archived_at IS NOT NULL"
        ).fetchone()["total"]
    assert stored == 4
    assert archived == 1
    assert journal(limit=10)["pagination"]["signals_total"] == 3
    assert archive_expired_signals(retention_days=30) == 0
