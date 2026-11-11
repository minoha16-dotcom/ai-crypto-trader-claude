"""Standalone, always-on process for paper-position monitoring.

Run this as a Render Background Worker using the same PostgreSQL DATABASE_URL
as the web service. A PostgreSQL session advisory lock elects one monitor leader
across web instances and workers, preventing duplicate paper exits.
"""
import asyncio
import logging
import signal

from .database import init_db
from .exchanges import registry
from .position_monitor import run_position_monitor

logger = logging.getLogger("ai_crypto_trader.monitor_worker")


async def serve():
    init_db()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Some local event loops do not implement POSIX signal handlers.
            pass

    logger.info("Starting dedicated paper-position monitor worker")
    try:
        await run_position_monitor(stop_event)
    finally:
        await registry.close()
        logger.info("Paper-position monitor worker stopped")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logger.info("Worker interrupted")


if __name__ == "__main__":
    main()
