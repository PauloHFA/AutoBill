"""Worker de reprocessamento da fila persistente (RNF10)."""

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autobill.config import settings
from autobill.db.models import BillOrigin
from autobill.db.repository import QueueRepository, db
from autobill.ingest.processor import ingest_bytes
from autobill.logging_config import get_logger

logger = get_logger(__name__)


async def process_retry_queue(bot: Any | None = None) -> int:
    """Reprocessa itens pendentes na `processing_queue` cujo próximo retry já venceu."""
    now = datetime.now(UTC)
    processed = 0

    async with db.session() as session:
        due_items = await QueueRepository(session).get_due_items(now)
        item_ids_and_paths = [
            (item.id, item.file_path, item.source, item.chat_id) for item in due_items
        ]

    for item_id, file_path_str, source_str, chat_id in item_ids_and_paths:
        path = Path(file_path_str)
        if not path.exists():
            async with db.session() as session:
                await QueueRepository(session).mark_failed(item_id, "Arquivo não encontrado")
            continue

        origin = (
            BillOrigin(source_str)
            if source_str in {o.value for o in BillOrigin}
            else BillOrigin.PASTA
        )
        target_chat = chat_id or (settings.allowed_chat_ids[0] if settings.allowed_chat_ids else 0)
        try:
            outcome = await ingest_bytes(
                file_bytes=path.read_bytes(),
                filename=path.name,
                origin=origin,
                chat_id=target_chat,
            )
            async with db.session() as session:
                q_repo = QueueRepository(session)
                if outcome.status in ("created", "duplicate_hash", "duplicate_linha", "rejected"):
                    await q_repo.mark_done(item_id)
                    processed += 1
                    if outcome.status == "created" and outcome.bill and bot and target_chat:
                        with contextlib.suppress(Exception):
                            await bot.send_message(
                                chat_id=target_chat,
                                text=f"🔄 Fatura #{outcome.bill.id} reprocessada com sucesso.",
                            )
                else:
                    await q_repo.mark_failed(item_id, outcome.reason or outcome.status)
        except Exception as exc:
            async with db.session() as session:
                await QueueRepository(session).mark_failed(item_id, str(exc))

    return processed
