"""Health check module and /healthz HTTP endpoint for AutoBill."""

import asyncio
import contextlib
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from autobill import __version__
from autobill.config import settings
from autobill.db.repository import QueueRepository, db
from autobill.logging_config import get_logger

logger = get_logger(__name__)

# Estado operacional em memória atualizado pela aplicação
runtime_status: dict[str, Any] = {
    "started_at": datetime.now(UTC).isoformat(),
    "last_processed_at": None,
    "scheduler_running": False,
    "next_alert_at": None,
    "recent_errors": [],
}


def record_processed() -> None:
    runtime_status["last_processed_at"] = datetime.now(UTC).isoformat()


def record_error(message: str) -> None:
    errors: list[dict[str, str]] = runtime_status["recent_errors"]
    errors.insert(0, {"at": datetime.now(UTC).isoformat(), "error": message[:250]})
    del errors[10:]


async def check_database() -> bool:
    """Check database connectivity."""
    try:
        if db.engine is None:
            await db.connect()
        async with db.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("database_health_check_failed")
        return False
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
            test_file = d / ".write_test"
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
    except Exception:
        logger.exception("data_directory_check_failed")
        return False
    return True


async def check_config() -> bool:
    """Check critical configuration."""
    try:
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
    return True


async def get_health_payload() -> tuple[bool, dict[str, Any]]:
    """Build diagnostic JSON payload for /healthz and /status."""
    db_ok = await check_database()
    dirs_ok = await check_data_directories()
    queue_pending = 0
    if db_ok:
        try:
            async with db.session() as session:
                queue_pending = await QueueRepository(session).count_pending()
        except Exception:
            queue_pending = 0

    healthy = db_ok and dirs_ok
    payload = {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "db": db_ok,
        "directories": dirs_ok,
        "queue_pending": queue_pending,
        "scheduler_running": runtime_status.get("scheduler_running", False),
        "last_processed_at": runtime_status.get("last_processed_at"),
        "next_alert_at": runtime_status.get("next_alert_at"),
        "recent_errors": runtime_status.get("recent_errors", [])[:5],
    }
    return (healthy, payload)


async def _handle_http_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        req_str = request_line.decode("ascii", errors="ignore")
        path = req_str.split(" ")[1] if len(req_str.split(" ")) >= 2 else "/"

        if path.startswith("/healthz"):
            healthy, payload = await get_health_payload()
            status_code = 200 if healthy else 503
            status_text = "OK" if healthy else "Service Unavailable"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            status_code = 404
            status_text = "Not Found"
            body = b'{"error":"not_found"}'

        crlf = chr(13) + "\n"
        headers = crlf.join(
            [
                f"HTTP/1.1 {status_code} {status_text}",
                "Content-Type: application/json; charset=utf-8",
                f"Content-Length: {len(body)}",
                "Connection: close",
                "",
                "",
            ]
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_health_server(
    host: str = "0.0.0.0", port: int | None = None
) -> asyncio.AbstractServer:
    """Start lightweight asyncio HTTP server exposing GET /healthz."""
    target_port = port if port is not None else settings.health_port
    server = await asyncio.start_server(_handle_http_client, host=host, port=target_port)
    logger.info("health_server_started", host=host, port=target_port)
    return server


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
