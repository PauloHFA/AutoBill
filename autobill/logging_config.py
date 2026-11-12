"""Logging configuration using structlog."""

import logging
import sys

import structlog
from structlog.types import EventDict, WrappedLogger

# Constant for masking
MASK_LENGTH = 4


def add_masking(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Mask sensitive data in logs."""
    # Mask linha_digitavel - show only last 4 digits
    if "linha_digitavel" in event_dict:
        linha = event_dict["linha_digitavel"]
        if isinstance(linha, str) and len(linha) > MASK_LENGTH:
            event_dict["linha_digitavel"] = "*" * (len(linha) - MASK_LENGTH) + linha[-MASK_LENGTH:]

    # Mask token
    if "token" in event_dict:
        event_dict["token"] = "***MASKED***"

    # Mask API keys
    for key in ["gemini_api_key", "imap_password", "telegram_bot_token"]:
        if key in event_dict:
            event_dict[key] = "***MASKED***"

    return event_dict


def setup_logging(json_logs: bool = True, log_level: str = "INFO") -> None:
    """Configure structlog for the application."""
    shared_processors = [
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
        processors = [*shared_processors, structlog.processors.JSONRenderer()]
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
    return structlog.get_logger(name)
