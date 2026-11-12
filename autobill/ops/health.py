"""Health check module for AutoBill."""

import asyncio
import contextlib
import sys

from autobill.config import settings
from autobill.db.repository import db
from autobill.logging_config import get_logger

logger = get_logger(__name__)


async def check_database() -> bool:
    """Check database connectivity."""
    try:
        await db.connect()
        async with db.session() as session:
            await session.execute("SELECT 1")
    except Exception:
        logger.exception("database_health_check_failed")
        return False
    else:
        return True


async def check_data_directories() -> bool:
    """Check data directories exist and are writable."""
    try:
        dirs = [
            settings.data_dir,
            settings.data_dir / "inbox",
            settings.data_dir / "rejeitados",
            settings.data_dir / "revisao",
            settings.data_dir / "processados",
            settings.data_dir / "backup",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            # Test write
            test_file = d / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
    except Exception:
        logger.exception("data_directory_check_failed")
        return False
    else:
        return True


async def check_config() -> bool:
    """Check critical configuration."""
    try:
        # Check required settings
        if not settings.telegram_bot_token or settings.telegram_bot_token == "your_bot_token_here":
            logger.error("missing_telegram_token")
            return False

        if not settings.allowed_chat_ids:
            logger.error("missing_allowed_chat_ids")
            return False

        if settings.extractor in ("cloud", "auto") and not settings.cloud_extraction_consent:
            logger.warning("cloud_extraction_consent_not_given")

        if settings.extractor == "cloud" and not settings.gemini_api_key:
            logger.error("missing_gemini_api_key_for_cloud_mode")
            return False
    except Exception:
        logger.exception("config_check_failed")
        return False
    else:
        return True


async def run_health_checks() -> int:
    """Run all health checks. Returns 0 if healthy, 1 otherwise."""
    logger.info("running_health_checks")

    checks = [
        ("config", check_config()),
        ("database", check_database()),
        ("data_directories", check_data_directories()),
    ]

    all_passed = True
    for name, check in checks:
        try:
            result = await check
            if result:
                logger.info("health_check_passed", check=name)
            else:
                logger.error("health_check_failed", check=name)
                all_passed = False
        except Exception:
            logger.exception("health_check_error", check=name)
            all_passed = False

    if all_passed:
        logger.info("all_health_checks_passed")
        return 0
    logger.error("some_health_checks_failed")
    return 1


def main() -> int:
    """Main entry point for health check."""
    with contextlib.suppress(asyncio.CancelledError):
        return asyncio.run(run_health_checks())
    return 0


if __name__ == "__main__":
    sys.exit(main())
