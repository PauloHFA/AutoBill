"""Política de retenção de arquivos originais, expurgo e limpeza total (LGPD)."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autobill.config import settings
from autobill.db.repository import BillRepository, db
from autobill.logging_config import get_logger

logger = get_logger(__name__)


async def cleanup_expired_files(retention_days: int | None = None) -> int:
    """
    Remove arquivos originais de faturas pagas há mais de `FILE_RETENTION_DAYS` dias,
    preservando os dados textuais no banco (RF24).
    """
    days = retention_days if retention_days is not None else settings.file_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0

    async with db.session() as session:
        repo = BillRepository(session)
        bills = await repo.get_paid_bills_for_file_retention(cutoff)
        for bill in bills:
            if bill.arquivo_path:
                path = Path(bill.arquivo_path)
                if path.exists():
                    path.unlink(missing_ok=True)
                bill.arquivo_path = None
                removed += 1
        await session.flush()

    if removed:
        logger.info("retention_files_cleaned", count=removed, retention_days=days)
    return removed


async def purge_expired_soft_deletes(days: int = 30) -> int:
    """Remove definitivamente faturas excluídas logicamente há mais de `days` dias (RF15)."""
    async with db.session() as session:
        repo = BillRepository(session)
        purged = await repo.purge_soft_deleted(days=days)
    if purged:
        logger.info("soft_deleted_bills_purged", count=purged, days=days)
    return purged


async def wipe_all_data(
    *,
    data_dir: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """
    Remove banco, arquivos armazenados e backups para atender /apagar_tudo (RF24).
    """
    target_data = data_dir or settings.data_dir
    target_db = db_path or settings.db_path
    files_removed = 0

    await db.close()

    for db_variant in (
        target_db,
        Path(f"{target_db}-wal"),
        Path(f"{target_db}-shm"),
    ):
        if db_variant.exists():
            db_variant.unlink(missing_ok=True)
            files_removed += 1

    for sub in ("inbox", "rejeitados", "revisao", "processados", "backup", "ingress"):
        folder = target_data / sub
        if folder.exists():
            for item in folder.rglob("*"):
                if item.is_file():
                    files_removed += 1
            shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)

    await db.connect()
    await db.create_tables()
    logger.warning("all_user_data_wiped", files_removed=files_removed)
    return {"files_removed": files_removed}
