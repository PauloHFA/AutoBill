"""Logging configuration using structlog."""

import logging
import sys
from typing import cast

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

# Constant for masking (RNF07: show only last 6 digits)
MASK_LENGTH = 6
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "gemini_api_key",
        "google_api_key",
        "imap_password",
        "telegram_bot_token",
        "password",
        "secret",
        "api_key",
    }
)


def mask_digits(value: str, visible_tail: int = MASK_LENGTH) -> str:
    """Mask a numeric line/barcode leaving only the last `visible_tail` digits."""
    if len(value) <= visible_tail:
        return "*" * len(value)
    return "*" * (len(value) - visible_tail) + value[-visible_tail:]


def add_masking(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Mask sensitive data in logs."""
    for field in ("linha_digitavel", "codigo_barras"):
        if field in event_dict:
            raw = event_dict[field]
            if isinstance(raw, str):
                event_dict[field] = mask_digits(raw, MASK_LENGTH)

    for key in list(event_dict.keys()):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "***MASKED***"

    return event_dict


def setup_logging(json_logs: bool = True, log_level: str = "INFO") -> None:
    """Configure structlog for the application."""
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        add_masking,
    ]

    if json_logs:
        processors: list[Processor] = [*shared_processors, structlog.processors.JSONRenderer()]
    else:
        processors = [*shared_processors, structlog.dev.ConsoleRenderer(colors=True)]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a logger instance."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
