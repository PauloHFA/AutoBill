"""Utility functions for Telegram bot."""

import logging
import time
from html import escape
from pathlib import Path

from telegram import CallbackQuery, Update
from telegram.ext import ContextTypes

from autobill.config import settings

logger = logging.getLogger(__name__)

BYTES_PER_UNIT = 1024


def get_user_id(update: Update | CallbackQuery) -> int | None:
    """Extract user ID from update."""
    user = update.from_user if isinstance(update, CallbackQuery) else update.effective_user
    return user.id if user else None


def get_chat_id(update: Update) -> int | None:
    """Extract chat ID from update."""
    if update.effective_chat:
        return update.effective_chat.id
    return None


def is_private_chat(update: Update) -> bool:
    """Check if update is from a private chat."""
    return bool(update.effective_chat and update.effective_chat.type == "private")


def is_authorized_user(user_id: int) -> bool:
    """Check if user is authorized."""
    authorized = settings.allowed_chat_ids
    if not authorized:
        return True  # No restriction configured
    return user_id in authorized


async def download_file(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> bytes | None:
    """Download a file from Telegram."""
    try:
        file = await context.bot.get_file(file_id)
        file_bytes = await file.download_as_bytearray()
        return bytes(file_bytes)
    except Exception:
        logger.exception("Failed to download file %s", file_id)
        return None


async def save_uploaded_file(
    file_bytes: bytes,
    original_filename: str,
    user_id: int,
) -> Path | None:
    """Save uploaded file to ingress directory."""
    try:
        ingress_dir = settings.data_dir / "ingress"
        ingress_dir.mkdir(parents=True, exist_ok=True)

        # Create user subdirectory
        user_dir = ingress_dir / str(user_id)
        user_dir.mkdir(exist_ok=True)

        # Generate safe filename
        safe_name = "".join(c for c in original_filename if c.isalnum() or c in "._-")
        if not safe_name:
            safe_name = "upload"

        # Add timestamp to avoid collisions
        timestamp = int(time.time() * 1000)
        name, separator, extension = safe_name.rpartition(".")
        filename = f"{name}_{timestamp}.{extension}" if separator else f"{safe_name}_{timestamp}"

        file_path = user_dir / filename
        file_path.write_bytes(file_bytes)

    except Exception:
        logger.exception("Failed to save uploaded file")
        return None
    else:
        return file_path


def get_file_extension(filename: str) -> str:
    """Get file extension in lowercase."""
    return Path(filename).suffix.lower()


def is_pdf_file(filename: str) -> bool:
    """Check if file is a PDF."""
    return get_file_extension(filename) == ".pdf"


def is_image_file(filename: str) -> bool:
    """Check if file is an image."""
    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
    return get_file_extension(filename) in image_extensions


def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format."""
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < BYTES_PER_UNIT:
            return f"{size:.1f} {unit}"
        size /= BYTES_PER_UNIT
    return f"{size:.1f} TB"


def truncate_text(text: str, max_length: int = 4000) -> str:
    """Truncate text to fit Telegram message limits."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return escape(text, quote=True)


def parse_callback_data(data: str) -> tuple[str, list[str]]:
    """Parse callback data into action and parameters."""
    parts = data.split(":")
    action = parts[0]
    params = parts[1:] if len(parts) > 1 else []
    return action, params


def build_callback_data(action: str, *params: str) -> str:
    """Build callback data string."""
    return ":".join([action, *params])
