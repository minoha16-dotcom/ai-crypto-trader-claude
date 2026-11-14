"""Server-side paper-position stop/target monitor.

The PostgreSQL session advisory lock ensures only one app process runs the
monitor when the web service is scaled to multiple instances/workers.
"""
import asyncio
import logging
import math
import os
import time
from contextlib import contextmanager
from starlette.concurrency import run_in_threadpool

from .config import settings
from .database import connect
from .exchanges import registry
from .paper import close_trade

logger = logging.getLogger(__name__)
MONITOR_LOCK_ID = 721904222
POLL_INTERVAL_SECONDS = max(2, int(os.getenv("POSITION_MONITOR_INTERVAL_SECONDS", "10")))


class StaleMarketPrice(ValueError):
    """Raised when an exchange ticker timestamp is older than the accepted age."""


def _validated_ticker_price(ticker):
    if not isinstance(ticker, dict):
        raise ValueError("Exchange returned an invalid ticker payload")
    raw_last = ticker.get("last")
    if raw_last is None:
        raise ValueError("Exchange returned no last price")
    try:
        last = float(raw_last)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Exchange returned a non-numeric last price") from None
    if not math.isfinite(last) or last <= 0:
        raise ValueError("Exchange returned an invalid last price")

    raw_timestamp = ticker.get("timestamp")
    if raw_timestamp is None:
        # Some CCXT adapters do not publish an exchange timestamp. Keep the
        # snapshot usable, but expose that its market-data age is unverified.
        return last, None, None
    try:
        timestamp = float(raw_timestamp)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Exchange returned a non-numeric ticker timestamp") from None
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("Exchange returned an invalid ticker timestamp")

    timestamp_ms = timestamp if timestamp >= 100_000_000_000 else timestamp * 1000.0
    now_ms = time.time() * 1000.0
    age_seconds = (now_ms - timestamp_ms) / 1000.0
    if age_seconds < -60.0:
        raise ValueError("Exchange ticker timestamp is more than 60 seconds ahead of local time")
    if age_seconds > float(settings.position_monitor_max_price_age_seconds):
        raise StaleMarketPrice(
            f"Exchange ticker is stale: age={age_seconds:.1f}s, "
            f"maximum={settings.position_monitor_max_price_age_seconds:.1f}s"
        )
    return last, int(timestamp_ms), max(0.0, age_seconds)


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
        url, connect_timeout=10,
        sslmode=os.getenv("DATABASE_SSLMODE", "require"), autocommit=True
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


async def monitor_open_positions_once(*, detailed=False):
    """Check open positions against public ticker snapshots.

    Timestamped snapshots older than the configured maximum age fail closed:
    the position stays open and the reason is returned in the detailed report.
    Missing exchange timestamps are allowed for compatibility, but counted as
    unverified so callers do not mistake them for measured-fresh market data.
    """
    def load_open_positions():
        with connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM trades WHERE status='OPEN' ORDER BY id ASC"
                ).fetchall()
            ]
    rows = await run_in_threadpool(load_open_positions)

    closed = 0
    failed = 0
    stale_prices = 0
    timestamp_unavailable = 0
    failures = []
    for row in rows:
        try:
            ticker = await registry.ticker(
                row["exchange"], row["symbol"], row["market_type"]
            )
            last, price_timestamp, price_age_seconds = _validated_ticker_price(ticker)
            if price_timestamp is None:
                timestamp_unavailable += 1

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
                result = await run_in_threadpool(close_trade, row["id"], last)
            except ValueError:
                # A dashboard/manual close may have won the race.
                logger.info(
                    "Position %s was already closed before monitor exit",
                    row["id"],
                )
                continue

            closed += 1
            logger.info(
                "Paper position auto-closed trade_id=%s exchange=%s symbol=%s "
                "market_type=%s reason=%s price=%.12g price_age_seconds=%s net_pnl=%.8g",
                row["id"], row["exchange"], row["symbol"], row["market_type"],
                reason, last,
                "unknown" if price_age_seconds is None else f"{price_age_seconds:.1f}",
                result["pnl"],
            )
        except Exception as exc:
            failed += 1
            is_stale = isinstance(exc, StaleMarketPrice)
            if is_stale:
                stale_prices += 1
            failure = {
                "trade_id": row.get("id"),
                "exchange": row.get("exchange"),
                "symbol": row.get("symbol"),
                "market_type": row.get("market_type"),
                "error_type": type(exc).__name__,
                "message": str(exc),
                "stale_price": is_stale,
                "upstream_status": getattr(exc, "status_code", None),
                "retry_after_seconds": getattr(exc, "retry_after", None),
            }
            failures.append(failure)
            logger.exception(
                "Paper position monitor check failed trade_id=%s exchange=%s "
                "symbol=%s market_type=%s error_type=%s stale_price=%s "
                "upstream_status=%s retry_after_seconds=%s",
                row.get("id"), row.get("exchange"), row.get("symbol"),
                row.get("market_type"), type(exc).__name__, is_stale,
                failure["upstream_status"], failure["retry_after_seconds"],
            )

    if detailed:
        return {
            "checked": len(rows),
            "closed": closed,
            "failed": failed,
            "stale_prices": stale_prices,
            "timestamp_unavailable": timestamp_unavailable,
            "remaining_open": max(0, len(rows) - closed),
            "failures": failures,
        }
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
                        report = await monitor_open_positions_once(detailed=True)
                        if report["failed"]:
                            logger.warning(
                                "Paper-position monitor cycle incomplete checked=%d "
                                "closed=%d failed=%d stale_prices=%d remaining_open=%d",
                                report["checked"], report["closed"], report["failed"],
                                report["stale_prices"], report["remaining_open"],
                            )
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
