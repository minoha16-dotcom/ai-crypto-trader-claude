from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3

from .config import settings


class _PostgresConnection:
    """Small compatibility adapter for the existing qmark-style SQL calls."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        statement = sql.strip()
        if statement.upper() == "BEGIN IMMEDIATE":
            # Serialize paper-trade risk checks across concurrent web requests.
            return self._connection.execute("SELECT pg_advisory_xact_lock(721904221)")
        statement = statement.replace("?", "%s")
        statement = statement.replace("date('now')", "CURRENT_DATE")
        statement = statement.replace("date(closed_at)", "CAST(closed_at AS DATE)")
        return self._connection.execute(statement, params)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _is_postgres():
    return settings.database_url.startswith(("postgres://", "postgresql://"))


@contextmanager
def connect():
    url = settings.database_url
    if _is_postgres():
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            url, row_factory=dict_row, connect_timeout=10,
            sslmode=os.getenv("DATABASE_SSLMODE", "require"),
        )
        db = _PostgresConnection(connection)
        try:
            yield db
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return

    if not url.startswith("sqlite:///"):
        raise ValueError("DATABASE_URL must use sqlite:/// or postgresql://")
    path = url.replace("sqlite:///", "", 1)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    if _is_postgres():
        schema = """
        CREATE TABLE IF NOT EXISTS signals(
            id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            exchange TEXT NOT NULL, symbol TEXT NOT NULL, market_type TEXT NOT NULL,
            timeframe TEXT NOT NULL, signal TEXT NOT NULL, score INTEGER NOT NULL,
            price DOUBLE PRECISION NOT NULL, rsi DOUBLE PRECISION, trend TEXT, payload TEXT NOT NULL,
            archived_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS trades(
            id BIGSERIAL PRIMARY KEY, opened_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMPTZ, exchange TEXT NOT NULL, symbol TEXT NOT NULL,
            market_type TEXT NOT NULL, side TEXT NOT NULL, quantity DOUBLE PRECISION NOT NULL,
            entry DOUBLE PRECISION NOT NULL, stop DOUBLE PRECISION NOT NULL, target DOUBLE PRECISION,
            exit_price DOUBLE PRECISION, pnl DOUBLE PRECISION DEFAULT 0,
            contract_size DOUBLE PRECISION NOT NULL DEFAULT 1, contract_type TEXT NOT NULL DEFAULT 'spot',
            status TEXT DEFAULT 'OPEN', signal_id BIGINT, notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS daily_equity_snapshots(
            trading_date DATE PRIMARY KEY, starting_equity DOUBLE PRECISION NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        """
    else:
        schema = """
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            exchange TEXT NOT NULL, symbol TEXT NOT NULL, market_type TEXT NOT NULL,
            timeframe TEXT NOT NULL, signal TEXT NOT NULL, score INTEGER NOT NULL,
            price REAL NOT NULL, rsi REAL, trend TEXT, payload TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT, exchange TEXT NOT NULL, symbol TEXT NOT NULL,
            market_type TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
            entry REAL NOT NULL, stop REAL NOT NULL, target REAL, exit_price REAL,
            pnl REAL DEFAULT 0, status TEXT DEFAULT 'OPEN', signal_id INTEGER,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS daily_equity_snapshots(
            trading_date TEXT PRIMARY KEY, starting_equity REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    with connect() as db:
        db.executescript(schema)
        if _is_postgres():
            db.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
            db.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS contract_size DOUBLE PRECISION NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS contract_type TEXT NOT NULL DEFAULT 'spot'")
            db.execute("UPDATE trades SET contract_type = 'legacy_base' WHERE market_type = 'futures' AND contract_type = 'spot'")
        else:
            signal_columns = {row["name"] for row in db.execute("PRAGMA table_info(signals)").fetchall()}
            if "archived_at" not in signal_columns:
                db.execute("ALTER TABLE signals ADD COLUMN archived_at TEXT")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(trades)").fetchall()}
            if "contract_size" not in columns:
                db.execute("ALTER TABLE trades ADD COLUMN contract_size REAL NOT NULL DEFAULT 1")
            if "contract_type" not in columns:
                db.execute("ALTER TABLE trades ADD COLUMN contract_type TEXT NOT NULL DEFAULT 'spot'")
            db.execute("UPDATE trades SET contract_type = 'legacy_base' WHERE market_type = 'futures' AND contract_type = 'spot'")
        # Support the monitor, account, journal and latest-signal read paths.
        db.execute("CREATE INDEX IF NOT EXISTS idx_trades_status_id ON trades(status, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trades_status_closed_at ON trades(status, closed_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trades_exchange_symbol_market ON trades(exchange, symbol, market_type, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_signals_market_symbol_timeframe_id ON signals(exchange, market_type, symbol, timeframe, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_signals_archived_id ON signals(archived_at, id)")


def archive_expired_signals(retention_days=None):
    """Soft-archive old signals; rows remain recoverable and are never deleted."""
    if retention_days is None:
        raw_days = os.getenv("SIGNAL_RETENTION_DAYS", "365")
        try:
            retention_days = int(raw_days)
        except (TypeError, ValueError):
            retention_days = 365
    retention_days = max(30, min(int(retention_days), 3650))
    cutoff_at = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff = cutoff_at if _is_postgres() else cutoff_at.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as db:
        cursor = db.execute(
            "UPDATE signals SET archived_at=CURRENT_TIMESTAMP "
            "WHERE archived_at IS NULL AND created_at < ?", (cutoff,)
        )
        return max(0, cursor.rowcount)
