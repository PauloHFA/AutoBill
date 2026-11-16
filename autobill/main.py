"""Main entry point for AutoBill (bot + scheduler + ingestores + /healthz)."""

import asyncio
import contextlib
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from autobill.alerts.scheduler import AlertScheduler
from autobill.bot.handlers import build_application, set_active_scheduler
from autobill.config import settings
from autobill.db.repository import db
from autobill.ingest.folder import FolderIngestor
from autobill.ingest.imap import ImapIngestor
from autobill.logging_config import get_logger, setup_logging
from autobill.ops.health import start_health_server

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Application lifespan manager: DB, /healthz, Telegram bot, scheduler e ingestores."""
    setup_logging(
        json_logs=settings.log_json,
        log_level=settings.log_level,
    )
    settings.ensure_directories()
    logger.info("starting_autobill", version="0.1.0")

    await db.connect()
    await db.create_tables()
    logger.info("database_connected", db_path=str(settings.db_path))

    health_server = await start_health_server(
        host="0.0.0.0",
        port=settings.health_port,
    )

    app = None
    bot = None
    if settings.telegram_bot_token:
        app = build_application(settings.telegram_bot_token)
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling(drop_pending_updates=False)
        bot = app.bot
        logger.info("telegram_bot_polling_started")
    else:
        logger.warning("telegram_bot_token_missing")

    scheduler = AlertScheduler(bot=bot)
    scheduler.start()
    set_active_scheduler(scheduler)
    await scheduler.run_daily_alert_cycle()

    folder_ingestor = FolderIngestor(bot=bot)
    folder_ingestor.start()

    imap_ingestor = ImapIngestor(bot=bot)
    if settings.imap_enabled:
        imap_ingestor.start()

    try:
        yield
    finally:
        logger.info("shutting_down")
        set_active_scheduler(None)
        await imap_ingestor.stop()
        await folder_ingestor.stop()
        scheduler.stop()

        if app is not None:
            if app.updater:
                with contextlib.suppress(Exception):
                    await app.updater.stop()
            with contextlib.suppress(Exception):
                await app.stop()
                await app.shutdown()

        health_server.close()
        await health_server.wait_closed()
        await db.close()
        logger.info("autobill_stopped")


async def main() -> None:
    """Main async entry point."""
    async with lifespan():
        stop_event = asyncio.Event()
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop_event.set)

        with contextlib.suppress(asyncio.CancelledError):
            await stop_event.wait()


def run() -> None:
    """Entrypoint síncrono para o script CLI `autobill`."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
