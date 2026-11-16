"""Monitoramento da pasta data/inbox/ com watchdog e estabilização de 5 s (RF01)."""

import asyncio
import contextlib
import shutil
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from autobill.alerts.formatter import get_alert_action_keyboard
from autobill.bot.formatters import format_bill_details
from autobill.bot.keyboards import get_review_keyboard
from autobill.config import settings
from autobill.db.models import BillOrigin, BillStatus
from autobill.ingest.processor import ingest_bytes
from autobill.logging_config import get_logger

logger = get_logger(__name__)

STABILITY_WAIT_SECONDS = 5.0


async def wait_for_stable_file(
    path: Path, stability_seconds: float = STABILITY_WAIT_SECONDS
) -> bool:
    """Aguarda até o arquivo parar de crescer/mudar por `stability_seconds`."""
    if not path.exists() or not path.is_file():
        return False
    try:
        last_size = path.stat().st_size
        stable_since = time.monotonic()
        while time.monotonic() - stable_since < stability_seconds:
            await asyncio.sleep(1.0)
            if not path.exists():
                return False
            current_size = path.stat().st_size
            if current_size != last_size or current_size == 0:
                last_size = current_size
                stable_since = time.monotonic()
    except OSError:
        return False
    return True


class _InboxEventHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]) -> None:
        self.loop = loop
        self.queue = queue

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and hasattr(event, "dest_path"):
            self.loop.call_soon_threadsafe(self.queue.put_nowait, Path(str(event.dest_path)))


class FolderIngestor:
    """Observa `data/inbox/` e processa novos boletos."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self.inbox_dir = settings.data_dir / "inbox"
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._in_flight: set[Path] = set()
        self._observer: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._scan_task: asyncio.Task[None] | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        settings.ensure_directories()
        self._running = True
        loop = asyncio.get_running_loop()

        handler = _InboxEventHandler(loop, self._queue)
        observer = Observer()
        observer.schedule(handler, str(self.inbox_dir), recursive=False)
        observer.start()
        self._observer = observer

        self._task = asyncio.create_task(self._worker_loop())
        self._scan_task = asyncio.create_task(self._periodic_scan_loop())
        logger.info("folder_ingestor_started", inbox_dir=str(self.inbox_dir))

    async def stop(self) -> None:
        self._running = False
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        for task in (self._task, self._scan_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _periodic_scan_loop(self) -> None:
        """Varredura periódica (até 60 s) como garantia extra além do watchdog."""
        while self._running:
            try:
                for path in sorted(self.inbox_dir.iterdir()):
                    if (
                        path.is_file()
                        and not path.name.startswith(".")
                        and path not in self._in_flight
                    ):
                        await self._queue.put(path)
            except Exception:
                logger.exception("inbox_scan_error")
            await asyncio.sleep(30)

    async def _worker_loop(self) -> None:
        while self._running:
            path = await self._queue.get()
            if path in self._in_flight or not path.exists() or path.name.startswith("."):
                continue
            self._in_flight.add(path)
            try:
                await self.process_inbox_file(path)
            finally:
                self._in_flight.discard(path)

    async def _notify_chats(self, text: str, reply_markup: Any | None = None) -> None:
        if self.bot is None:
            return
        for chat_id in settings.allowed_chat_ids:
            with contextlib.suppress(Exception):
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )

    async def process_inbox_file(self, path: Path) -> None:
        if not await wait_for_stable_file(path):
            return

        file_bytes = path.read_bytes()
        target_chat = settings.allowed_chat_ids[0] if settings.allowed_chat_ids else 0
        outcome = await ingest_bytes(
            file_bytes=file_bytes,
            filename=path.name,
            origin=BillOrigin.PASTA,
            chat_id=target_chat,
        )

        # Remove arquivo original de inbox/ (já copiado para processados/revisao/rejeitados)
        if outcome.status == "rejected" and outcome.stored_path:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            await self._notify_chats(
                f"📁 <b>Arquivo rejeitado da pasta inbox</b> (<code>{path.name}</code>)\n"
                f"Motivo: {outcome.reason}"
            )
            return

        if outcome.status in ("duplicate_hash", "duplicate_linha"):
            with contextlib.suppress(OSError):
                dest = settings.data_dir / "rejeitados" / path.name
                shutil.move(str(path), str(dest))
            await self._notify_chats(
                f"🔁 <b>Duplicata ignorada da pasta inbox</b> (<code>{path.name}</code>)\n"
                f"{outcome.reason}"
            )
            return

        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

        if outcome.status == "password_required":
            await self._notify_chats(
                f"🔒 <b>PDF protegido por senha</b> recebido em inbox/ (<code>{path.name}</code>).\n"
                f"Reenvie o arquivo diretamente no Telegram para informar a senha."
            )
            return

        if outcome.status == "created" and outcome.bill is not None:
            bill = outcome.bill
            if bill.status == BillStatus.PENDENTE:
                await self._notify_chats(
                    f"✅ <b>Fatura importada automaticamente de inbox/</b>\n\n"
                    f"{format_bill_details(bill)}",
                    reply_markup=get_alert_action_keyboard(bill),
                )
            else:
                await self._notify_chats(
                    f"✏️ <b>Fatura importada de inbox/ aguardando revisão</b>\n\n"
                    f"{format_bill_details(bill)}",
                    reply_markup=get_review_keyboard(bill.id),
                )
