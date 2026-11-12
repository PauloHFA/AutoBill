"""Main entry point for AutoBill."""

import asyncio
import contextlib
import signal
import sys
from contextlib import asynccontextmanager

from autobill.config import settings
from autobill.db.repository import db
from autobill.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan():
    """Application lifespan manager."""
    # Startup
    setup_logging(json_logs=settings.log_json, log_level=settings.log_level)
    logger.info("starting_autobill", version="0.1.0")

    await db.connect()
    await db.create_tables()

    logger.info("database_connected", db_path=str(settings.db_path))

    yield

    # Shutdown
    logger.info("shutting_down")
    await db.close()


async def main() -> None:
    """Main application entry point."""
    async with lifespan():
        # Keep running until signal
        stop_event = asyncio.Event()

        def signal_handler() -> None:
            logger.info("signal_received", signal="SIGINT/SIGTERM")
            stop_event.set()

        # Register signal handlers
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, signal_handler)
        else:
            # Windows doesn't support add_signal_handler
            pass

        with contextlib.suppress(asyncio.CancelledError):
            await stop_event.wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
