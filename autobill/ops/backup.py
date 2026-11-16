"""Backup automático do banco SQLite com retenção configurável."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from autobill.config import settings
from autobill.logging_config import get_logger

logger = get_logger(__name__)


def create_backup(
    *,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    retention_count: int | None = None,
) -> Path | None:
    """
    Cria cópia consistente (SQLite online backup) em `data/backup/` e remove cópias excedentes.
    """
    source = db_path or settings.db_path
    if not source.exists():
        logger.warning("backup_skipped_missing_db", db_path=str(source))
        return None

    target_dir = backup_dir or (settings.data_dir / "backup")
    target_dir.mkdir(parents=True, exist_ok=True)
    keep = retention_count if retention_count is not None else settings.backup_retention_count

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = target_dir / f"autobill_{stamp}.db"

    src_conn = sqlite3.connect(str(source), timeout=30)
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # Retenção: mantém apenas os N backups mais recentes
    backups = sorted(
        target_dir.glob("autobill_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        old.unlink(missing_ok=True)

    logger.info("database_backup_created", path=str(dest), retained=min(len(backups), keep))
    return dest
