"""Utility functions for Telegram bot."""

import io
import logging
import os
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from autobill.config import settings

logger = logging.getLogger(__name__)


def get_user_id(update: Update) -> Optional[int]:
    """Extract user ID from update."""
    if update.effective_user:
        return update.effective_user.id
    return None


def get_chat_id(update: Update) -> Optional[int]:
    """Extract chat ID from update."""
    if update.effective_chat:
        return update.effective_chat.id
    return None


def is_private_chat(update: Update) -> bool:
    """Check if update is from a private chat."""
    return update.effective_chat and update.effective_chat.type == "private"


def is_authorized_user(user_id: int) -> bool:
    """Check if user is authorized."""
    authorized = settings.telegram_authorized_users
    if not authorized:
        return True  # No restriction configured
    return user_id in authorized


async def download_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> Optional[bytes]:
    """Download a file from Telegram."""
    try:
        file = await context.bot.get_file(file_id)
        file_bytes = await file.download_as_bytearray()
        return bytes(file_bytes)
    except Exception as e:
        logger.error(f"Failed to download file {file_id}: {e}")
        return None


async def save_uploaded_file(
    file_bytes: bytes,
    original_filename: str,
    user_id: int,
) -> Optional[Path]:
    """Save uploaded file to ingress directory."""
    try:
        ingress_dir = Path(settings.ingress_dir)
        ingress_dir.mkdir(parents=True, exist_ok=True)

        # Create user subdirectory
        user_dir = ingress_dir / str(user_id)
        user_dir.mkdir(exist_ok=True)

        # Generate safe filename
        safe_name = "".join(c for c in original_filename if c.isalnum() or c in "._-")
        if not safe_name:
            safe_name = "upload"

        # Add timestamp to avoid collisions
        import time
        timestamp = int(time.time() * 1000)
        name_parts = safe_name.rsplit(".", 1)
        if len(name_parts) == 2:
            filename = f"{name_parts[0]}_{timestamp}.{name_parts[1]}"
        else:
            filename = f"{safe_name}_{timestamp}"

        file_path = user_dir / filename
        file_path.write_bytes(file_bytes)

        return file_path
    except Exception as e:
        logger.error(f"Failed to save uploaded file: {e}")
        return None


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
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def truncate_text(text: str, max_length: int = 4000) -> str:
    """Truncate text to fit Telegram message limits."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&")
        .replace("<", "<")
        .replace(">", ">")
        .replace('"', """)
        .replace("'", "'")
    )


def parse_callback_data(data: str) -> tuple[str, list[str]]:
    """Parse callback data into action and parameters."""
    parts = data.split(":")
    action = parts[0]
    params = parts[1:] if len(parts) > 1 else []
    return action, params


def build_callback_data(action: str, *params: str) -> str:
    """Build callback data string."""
    return ":".join([action] + list(params))