"""Server-side paper-position stop/target monitor.

The PostgreSQL session advisory lock ensures only one app process runs the
monitor when the web service is scaled to multiple instances/workers.
"""
import asyncio
import logging
import math
import os
from contextlib import contextmanager

from .config import settings
from .database import connect
from .exchanges import registry
from .paper import close_trade

logger = logging.getLogger(__name__)
MONITOR_LOCK_ID = 721904222
POLL_INTERVAL_SECONDS = max(2, int(os.getenv("POSITION_MONITOR_INTERVAL_SECONDS", "10")))


@contextmanager
def _monitor_leader_lock():
    """Hold a PostgreSQL session lock for the lifetime of the monitor loop."""
    url = settings.database_url
    if not url.startswith(("postgres://", "postgresql://")):
        # SQLite is used for local development/tests. Render production uses
        # PostgreSQL, where the advisory lock provides cross-process exclusion.
        yield True
        return

    import psycopg

    connection = psycopg.connect(
        url, connect_timeout=10, sslmode="require", autocommit=True
    )
    acquired = False
    try:
        acquired = bool(
            connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (MONITOR_LOCK_ID,)
            ).fetchone()[0]
        )
        yield acquired
    finally:
        if acquired:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(%s)", (MONITOR_LOCK_ID,)
                )
            except Exception:
                logger.exception("Could not explicitly release position-monitor lock")
        connection.close()


async def monitor_open_positions_once():
    """Check every open paper position once and close crossed SL/TP positions."""
    with connect() as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY id ASC"
            ).fetchall()
        ]

    closed = 0
    for row in rows:
        try:
            ticker = await registry.ticker(
                row["exchange"], row["symbol"], row["market_type"]
            )
            raw_last = ticker.get("last")
            if raw_last is None:
                raise ValueError("Exchange returned no last price")
            last = float(raw_last)
            if not math.isfinite(last) or last <= 0:
                raise ValueError("Exchange returned an invalid last price")

            side = row["side"]
            stop = float(row["stop"])
            target = float(row["target"]) if row.get("target") is not None else None
            stop_hit = (
                (side == "LONG" and last <= stop)
                or (side == "SHORT" and last >= stop)
            )
            target_hit = target is not None and (
                (side == "LONG" and last >= target)
                or (side == "SHORT" and last <= target)
            )
            if not stop_hit and not target_hit:
                continue

            reason = "STOP" if stop_hit else "TARGET"
            try:
                result = close_trade(row["id"], last)
            except ValueError:
                # A dashboard/manual close may have won the race.
                logger.info(
                    "Position %s was already closed before monitor exit",
                    row["id"],
                )
                continue

            closed += 1
            logger.info(
                "Paper position %s auto-closed by %s at %.12g (net P&L %.8g)",
                row["id"], reason, last, result["pnl"],
            )
        except Exception:
            logger.exception(
                "Position monitor could not evaluate open position %s",
                row.get("id"),
            )
    return closed


async def run_position_monitor(stop_event: asyncio.Event):
    """Run continuously without depending on dashboard/API polling."""
    while not stop_event.is_set():
        try:
            with _monitor_leader_lock() as is_leader:
                if not is_leader:
                    logger.info(
                        "Another app process owns the position monitor; retrying"
                    )
                else:
                    logger.info(
                        "Server-side position monitor started (interval=%ss)",
                        POLL_INTERVAL_SECONDS,
                    )
                    while not stop_event.is_set():
                        await monitor_open_positions_once()
                        try:
                            await asyncio.wait_for(
                                stop_event.wait(),
                                timeout=POLL_INTERVAL_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            pass
        except Exception:
            logger.exception("Position monitor loop failed; it will retry")
        if not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
