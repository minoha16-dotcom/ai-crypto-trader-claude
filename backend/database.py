from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .config import settings


def _path():
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise ValueError("V2 starter supports SQLite only")
    return url.replace("sqlite:///", "", 1)


@contextmanager
def connect():
    path = _path()
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
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            exchange TEXT NOT NULL, symbol TEXT NOT NULL, market_type TEXT NOT NULL,
            timeframe TEXT NOT NULL, signal TEXT NOT NULL, score INTEGER NOT NULL,
            price REAL NOT NULL, rsi REAL, trend TEXT, payload TEXT NOT NULL
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
        """)
