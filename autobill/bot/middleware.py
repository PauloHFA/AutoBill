"""Middleware for Telegram bot."""

import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from autobill.bot.state import state_manager
from autobill.config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple rate limiter per user."""

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[int, list[float]] = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        """Check if user is within rate limit."""
        now = time.time()
        user_requests = self._requests[user_id]

        # Remove old requests outside window
        user_requests[:] = [t for t in user_requests if now - t < self.window_seconds]

        if len(user_requests) >= self.max_requests:
            return False

        user_requests.append(now)
        return True

    def get_remaining(self, user_id: int) -> int:
        """Get remaining requests in current window."""
        now = time.time()
        user_requests = self._requests[user_id]
        user_requests[:] = [t for t in user_requests if now - t < self.window_seconds]
        return max(0, self.max_requests - len(user_requests))


rate_limiter = RateLimiter()


async def auth_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    next_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Any:
    """Authentication middleware - checks if user is authorized."""
    if not update.effective_user:
        return None

    user_id = update.effective_user.id

    # Check if bot has authorized users configured
    authorized_users = settings.telegram_authorized_users
    if authorized_users and user_id not in authorized_users:
        if update.message:
            await update.message.reply_text(
                "🚫 <b>Acesso negado</b>\n\n"
                "Este bot é privado. Contate o administrador para autorização.",
                parse_mode="HTML",
            )
        elif update.callback_query:
            await update.callback_query.answer("🚫 Acesso negado", show_alert=True)
        logger.warning(f"Unauthorized access attempt from user {user_id}")
        return None

    return await next_handler(update, context)


async def rate_limit_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    next_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Any:
    """Rate limiting middleware."""
    if not update.effective_user:
        return None

    user_id = update.effective_user.id

    if not rate_limiter.is_allowed(user_id):
        remaining = rate_limiter.get_remaining(user_id)
        if update.message:
            await update.message.reply_text(
                f"⏱ <b>Muitas requisições</b>\n\n"
                f"Aguarde um momento antes de enviar mais comandos.\n"
                f"Restam: {remaining} requisições/minuto",
                parse_mode="HTML",
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "⏱ Muitas requisições, aguarde...",
                show_alert=True,
            )
        logger.warning(f"Rate limit exceeded for user {user_id}")
        return None

    return await next_handler(update, context)


async def logging_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    next_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Any:
    """Logging middleware for all updates."""
    start_time = time.time()

    user = update.effective_user
    chat = update.effective_chat

    log_data = {
        "user_id": user.id if user else None,
        "username": user.username if user else None,
        "chat_id": chat.id if chat else None,
        "chat_type": chat.type if chat else None,
    }

    if update.message:
        log_data["message_type"] = "text" if update.message.text else "media"
        if update.message.text:
            log_data["command"] = update.message.text.split()[0] if update.message.text.startswith("/") else None
    elif update.callback_query:
        log_data["callback_data"] = update.callback_query.data[:50]

    try:
        result = await next_handler(update, context)
        duration = time.time() - start_time
        logger.info(
            "Update processed",
            extra={**log_data, "duration_ms": round(duration * 1000, 2)},
        )
        return result
    except Exception as e:
        duration = time.time() - start_time
        logger.exception(
            "Update processing failed",
            extra={**log_data, "duration_ms": round(duration * 1000, 2), "error": str(e)},
        )
        raise


async def state_cleanup_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    next_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Any:
    """Periodic state cleanup middleware."""
    # Run cleanup occasionally (1% chance per update)
    import random
    if random.random() < 0.01:
        removed = state_manager.cleanup_old_states(max_age_hours=24)
        if removed:
            logger.info(f"Cleaned up {removed} old user states")

    return await next_handler(update, context)


def apply_middlewares(application) -> None:
    """Apply all middlewares to the application."""
    # Order matters: auth -> rate_limit -> logging -> state_cleanup
    application.add_handler(
        type("AuthMiddleware", (), {"__call__": auth_middleware})(),
        group=-10,
    )
    application.add_handler(
        type("RateLimitMiddleware", (), {"__call__": rate_limit_middleware})(),
        group=-9,
    )
    application.add_handler(
        type("LoggingMiddleware", (), {"__call__": logging_middleware})(),
        group=-8,
    )
    application.add_handler(
        type("StateCleanupMiddleware", (), {"__call__": state_cleanup_middleware})(),
        group=-7,
    )
