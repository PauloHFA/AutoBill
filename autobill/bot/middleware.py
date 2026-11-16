"""Middleware handlers for Telegram bot (python-telegram-bot TypeHandler)."""

import logging
import random
import time
from collections import defaultdict
from typing import Any

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, TypeHandler

from autobill.bot.state import state_manager
from autobill.bot.utils import get_chat_id, get_user_id, is_authorized_chat

logger = logging.getLogger(__name__)

STATE_CLEANUP_CHANCE = 0.01


class RateLimiter:
    """Simple rate limiter per user/chat."""

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[int, list[float]] = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        """Check if user is within rate limit."""
        now = time.time()
        user_requests = self._requests[user_id]
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


async def auth_middleware(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verifica autorização do chat/usuário; bloqueia sem resposta quando não autorizado (RF22)."""
    chat_id = get_chat_id(update)
    user_id = get_user_id(update)
    if not is_authorized_chat(chat_id, user_id, strict=True):
        logger.warning(
            "Unauthorized access attempt ignored: chat_id=%s user_id=%s",
            chat_id,
            user_id,
        )
        raise ApplicationHandlerStop


async def rate_limit_middleware(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rate limiting middleware."""
    user_id = get_user_id(update)
    if user_id is None:
        return
    if not rate_limiter.is_allowed(user_id):
        remaining = rate_limiter.get_remaining(user_id)
        if update.message:
            await update.message.reply_text(
                f"⏱ <b>Muitas requisições</b>\n\nAguarde um momento. Restam: {remaining} req/min.",
                parse_mode="HTML",
            )
        elif update.callback_query:
            await update.callback_query.answer("⏱ Muitas requisições, aguarde...", show_alert=True)
        logger.warning("Rate limit exceeded for user %s", user_id)
        raise ApplicationHandlerStop


async def logging_middleware(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Structured update logging & periodic state cleanup."""
    user = update.effective_user
    chat = update.effective_chat
    command = None
    if update.message and update.message.text and update.message.text.startswith("/"):
        command = update.message.text.split()[0]

    logger.info(
        "update_received user_id=%s chat_id=%s command=%s",
        user.id if user else None,
        chat.id if chat else None,
        command,
    )
    if random.random() < STATE_CLEANUP_CHANCE:
        removed = state_manager.cleanup_old_states(max_age_hours=24)
        if removed:
            logger.info("Cleaned up %d old user states", removed)


def apply_middlewares(application: Any) -> None:
    """Apply PTB TypeHandlers ordered by group priority."""
    application.add_handler(TypeHandler(Update, auth_middleware), group=-10)
    application.add_handler(TypeHandler(Update, rate_limit_middleware), group=-9)
    application.add_handler(TypeHandler(Update, logging_middleware), group=-8)
