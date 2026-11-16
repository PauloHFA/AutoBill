"""Ingestão opcional de anexos de faturas via IMAP (RF03)."""

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from imap_tools import AND, MailBox

from autobill.bot.formatters import format_bill_details
from autobill.bot.keyboards import get_review_keyboard
from autobill.config import settings
from autobill.db.models import BillOrigin, BillStatus
from autobill.ingest.processor import SUPPORTED_EXTENSIONS, ingest_bytes
from autobill.logging_config import get_logger
from autobill.ops.health import record_error

logger = get_logger(__name__)


def _fetch_unread_attachments() -> list[tuple[str, str, bytes]]:
    """
    Conecta no servidor IMAP de forma síncrona e retorna lista de (uid, filename, bytes).
    Ignora e-mails sem anexos suportados ou de remetentes fora de ALLOWED_SENDERS.
    """
    if not settings.imap_user or not settings.imap_password:
        return []

    collected: list[tuple[str, str, bytes]] = []
    allowed = set(settings.allowed_senders)

    with MailBox(settings.imap_host).login(settings.imap_user, settings.imap_password) as mailbox:
        for msg in mailbox.fetch(AND(seen=False), mark_seen=False):
            sender = (msg.from_ or "").strip().lower()
            if allowed and sender not in allowed:
                logger.info("imap_sender_ignored", sender=sender)
                continue

            supported = [
                att
                for att in msg.attachments
                if Path(att.filename or "").suffix.lower() in SUPPORTED_EXTENSIONS
            ]
            if not supported:
                # Ignora e-mail sem anexo suportado sem gravar nada
                continue

            collected.extend(
                (str(msg.uid), att.filename or "fatura.pdf", att.payload) for att in supported
            )

            # Marca como lido e tenta mover para pasta "AutoBill"
            if msg.uid:
                mailbox.flag(msg.uid, "\\Seen", True)
                with contextlib.suppress(Exception):
                    if not mailbox.folder.exists("AutoBill"):
                        mailbox.folder.create("AutoBill")
                    mailbox.move(msg.uid, "AutoBill")

    return collected


class ImapIngestor:
    """Worker periódico de polling IMAP."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not settings.imap_enabled:
            logger.info("imap_ingestor_disabled")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("imap_ingestor_started", host=settings.imap_host)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def poll_once(self) -> int:
        attachments = await asyncio.to_thread(_fetch_unread_attachments)
        processed = 0
        target_chat = settings.allowed_chat_ids[0] if settings.allowed_chat_ids else 0
        for _uid, filename, payload in attachments:
            outcome = await ingest_bytes(
                file_bytes=payload,
                filename=filename,
                origin=BillOrigin.EMAIL,
                chat_id=target_chat,
            )
            if outcome.status == "created" and outcome.bill is not None:
                processed += 1
                if self.bot and target_chat and outcome.bill.status == BillStatus.EM_REVISAO:
                    with contextlib.suppress(Exception):
                        await self.bot.send_message(
                            chat_id=target_chat,
                            text=(
                                f"📧 <b>Fatura recebida por e-mail aguardando revisão</b>\n\n"
                                f"{format_bill_details(outcome.bill)}"
                            ),
                            parse_mode="HTML",
                            reply_markup=get_review_keyboard(outcome.bill.id),
                        )
        return processed

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                record_error(f"IMAP error: {exc}")
                logger.exception("imap_poll_failed")
            await asyncio.sleep(max(15, settings.imap_poll_seconds))
