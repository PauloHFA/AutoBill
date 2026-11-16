"""Pipeline unificado de ingestão de arquivos (pasta, Telegram, e-mail)."""

import asyncio
import hashlib
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autobill.config import settings
from autobill.db.models import Bill, BillOrigin, BillStatus
from autobill.db.repository import (
    AuditRepository,
    BillRepository,
    QueueRepository,
    RecurringRepository,
    SettingsRepository,
    db,
)
from autobill.extract.pdf_text import PDFPasswordRequiredError
from autobill.extract.pipeline import ExtractionConfig, ExtractionPipeline, ExtractionResult
from autobill.logging_config import get_logger
from autobill.ops.health import record_error, record_processed

logger = get_logger(__name__)

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp"})
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB


@dataclass
class IngestionOutcome:
    """Resultado completo do processamento de um arquivo recebido."""

    status: str  # created, duplicate_hash, duplicate_linha, duplicate_heuristic, rejected, password_required, queued
    bill: Bill | None = None
    duplicate_bill: Bill | None = None
    extraction: ExtractionResult | None = None
    stored_path: Path | None = None
    reason: str | None = None


def _safe_filename(filename: str) -> str:
    safe = "".join(c for c in filename if c.isalnum() or c in "._-")
    return safe or "fatura.pdf"


def _store_file(file_bytes: bytes, filename: str, subfolder: str, file_hash: str) -> Path:
    target_dir = settings.data_dir / subfolder
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(filename)
    stem = Path(safe).stem[:40]
    ext = Path(safe).suffix.lower() or ".bin"
    stamp = int(time.time() * 1000)
    dest = target_dir / f"{stem}_{file_hash[:8]}_{stamp}{ext}"
    dest.write_bytes(file_bytes)
    return dest


async def ingest_bytes(
    *,
    file_bytes: bytes,
    filename: str,
    origin: BillOrigin,
    chat_id: int,
    password: str | None = None,
    force_duplicate: bool = False,
) -> IngestionOutcome:
    """
    Processa bytes de uma fatura recebida por qualquer canal:
    1. Valida extensão e tamanho (<= 20 MB).
    2. Calcula SHA-256 e verifica duplicidade por hash.
    3. Executa pipeline de extração (respeitando modo e consentimento do chat).
    4. Trata PDF protegido por senha.
    5. Verifica duplicidade por linha digitável ou heurística.
    6. Salva em `processados/` ou `revisao/` e registra no banco + auditoria + recorrência.
    """
    settings.ensure_directories()
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        rejected_path = _store_file(
            file_bytes, filename, "rejeitados", hashlib.sha256(file_bytes).hexdigest()
        )
        return IngestionOutcome(
            status="rejected",
            stored_path=rejected_path,
            reason=f"Formato não suportado ({ext or 'sem extensão'}). Use PDF, PNG, JPG ou WEBP.",
        )

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        rejected_path = _store_file(
            file_bytes, filename, "rejeitados", hashlib.sha256(file_bytes).hexdigest()
        )
        return IngestionOutcome(
            status="rejected",
            stored_path=rejected_path,
            reason="Arquivo excede o limite máximo de 20 MB.",
        )

    file_hash = hashlib.sha256(file_bytes).hexdigest()

    async with db.session() as session:
        bill_repo = BillRepository(session)
        existing_hash = await bill_repo.get_by_hash(file_hash, user_id=chat_id)
        if existing_hash is not None:
            return IngestionOutcome(
                status="duplicate_hash",
                duplicate_bill=existing_hash,
                reason=f"Arquivo idêntico já cadastrado (#{existing_hash.id}).",
            )

        user_cfg = await SettingsRepository(session).get_or_create(chat_id)
        extractor_mode = user_cfg.extractor_mode
        cloud_consent = user_cfg.cloud_consent
        auto_confirm = user_cfg.auto_confirm

    # Salva temporariamente em revisao/ durante extração
    temp_path = _store_file(file_bytes, filename, "revisao", file_hash)

    pipeline = ExtractionPipeline(
        ExtractionConfig(
            extractor_mode=extractor_mode,
            cloud_consent=cloud_consent,
            pdf_password=password,
        )
    )

    try:
        extraction = await asyncio.to_thread(pipeline.extract, temp_path)
    except PDFPasswordRequiredError:
        return IngestionOutcome(
            status="password_required",
            stored_path=temp_path,
            reason="PDF protegido por senha.",
        )
    except Exception as exc:
        record_error(f"Extraction error: {exc}")
        async with db.session() as session:
            await QueueRepository(session).enqueue(
                file_hash=file_hash,
                file_path=str(temp_path),
                source=origin.value,
                chat_id=chat_id,
                error=str(exc),
            )
        return IngestionOutcome(
            status="queued",
            stored_path=temp_path,
            reason=f"Erro temporário na extração; enfileirado para reprocessamento ({exc}).",
        )

    if not extraction.is_valid():
        rejected_path = settings.data_dir / "rejeitados" / temp_path.name
        shutil.move(str(temp_path), str(rejected_path))
        return IngestionOutcome(
            status="rejected",
            extraction=extraction,
            stored_path=rejected_path,
            reason="Não foi possível extrair dados mínimos do documento.",
        )

    async with db.session() as session:
        bill_repo = BillRepository(session)
        if extraction.linha_digitavel:
            dup_linha = await bill_repo.get_by_linha_digitavel(
                extraction.linha_digitavel, user_id=chat_id
            )
            if dup_linha is not None:
                temp_path.unlink(missing_ok=True)
                return IngestionOutcome(
                    status="duplicate_linha",
                    duplicate_bill=dup_linha,
                    extraction=extraction,
                    reason=f"Linha digitável já cadastrada na fatura #{dup_linha.id}.",
                )

        if not extraction.linha_digitavel and not force_duplicate:
            due_dt = (
                datetime.fromisoformat(extraction.vencimento).date()
                if extraction.vencimento
                else None
            )
            dup_heur = await bill_repo.find_heuristic_duplicate(
                user_id=chat_id,
                beneficiario=extraction.beneficiario,
                valor_centavos=extraction.valor,
                vencimento=due_dt,
            )
            if dup_heur is not None:
                return IngestionOutcome(
                    status="duplicate_heuristic",
                    duplicate_bill=dup_heur,
                    extraction=extraction,
                    stored_path=temp_path,
                    reason=(
                        f"Possível duplicata da fatura #{dup_heur.id} "
                        f"(mesmo beneficiário, valor e vencimento)."
                    ),
                )

        # Decide pasta final conforme confirmação automática
        will_auto_confirm = auto_confirm and extraction.can_auto_confirm()
        final_path = temp_path
        if will_auto_confirm:
            dest = settings.data_dir / "processados" / temp_path.name
            shutil.move(str(temp_path), str(dest))
            final_path = dest

        bill = await bill_repo.create_bill_from_extraction(
            user_id=chat_id,
            extraction_result=extraction,
            source_file=str(final_path),
            source_hash=file_hash,
            origin=origin,
            auto_approve=will_auto_confirm,
        )
        await RecurringRepository(session).detect_and_upsert(bill)
        await AuditRepository(session).log_action(
            chat_id=chat_id,
            action="ingest_auto" if bill.status == BillStatus.PENDENTE else "ingest_review",
            bill_id=bill.id,
            details=f"origem={origin.value} metodo={extraction.method_used}",
        )

    record_processed()
    logger.info(
        "bill_ingested",
        bill_id=bill.id,
        origin=origin.value,
        status=bill.status.value,
        linha_digitavel=bill.linha_digitavel or "",
    )
    return IngestionOutcome(
        status="created",
        bill=bill,
        extraction=extraction,
        stored_path=final_path,
    )
