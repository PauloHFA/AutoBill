"""Ingestão multi-canal (pasta inbox, Telegram, IMAP e fila de retry)."""

from autobill.ingest.folder import FolderIngestor
from autobill.ingest.imap import ImapIngestor
from autobill.ingest.processor import IngestionOutcome, ingest_bytes
from autobill.ingest.queue import process_retry_queue

__all__ = [
    "FolderIngestor",
    "ImapIngestor",
    "IngestionOutcome",
    "ingest_bytes",
    "process_retry_queue",
]
