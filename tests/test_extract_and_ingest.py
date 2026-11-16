"""Tests for logging masking, extraction pipeline, and unified ingestion."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from pypdf import PdfWriter

from autobill.config import settings
from autobill.db.models import BillOrigin, BillStatus
from autobill.extract.llm_vision import (
    extract_with_llm_vision,
    extract_with_ollama_vision,
    get_daily_llm_usage,
)
from autobill.extract.ocr import _preprocess_for_ocr
from autobill.extract.pdf_text import (
    PDFPasswordRequiredError,
    extract_boleto_fields_from_text,
    extract_text_from_pdf,
)
from autobill.extract.pipeline import ExtractionConfig, ExtractionPipeline, ExtractionResult
from autobill.ingest.folder import FolderIngestor, wait_for_stable_file
from autobill.ingest.imap import ImapIngestor
from autobill.ingest.processor import ingest_bytes
from autobill.ingest.queue import process_retry_queue
from autobill.logging_config import add_masking, mask_digits, setup_logging
from autobill.rules.boleto import codigo_barras_para_linha
from autobill.rules.modulo11 import modulo11_bancario


def _build_valid_linha(fator: int = 1400, valor: int = 15688) -> str:
    sem_dv = f"3419{fator:04d}{valor:010d}" + "1234567890123456789012345"
    dv = modulo11_bancario(sem_dv)
    cb = f"{sem_dv[:4]}{dv}{sem_dv[4:]}"
    linha = codigo_barras_para_linha(cb)
    assert linha is not None
    return linha


def test_logging_masking_last_6_digits() -> None:
    """RNF07: mascaramento preserva apenas os últimos 6 dígitos."""
    setup_logging(json_logs=False, log_level="INFO")
    linha = "34191790010104351004791020150008291070026000"
    masked = mask_digits(linha)
    assert masked.endswith("026000")
    assert masked.startswith("*")

    d = add_masking(
        None,  # type: ignore[arg-type]
        "info",
        {"linha_digitavel": linha, "telegram_bot_token": "secret123", "other": "ok"},
    )
    assert d["telegram_bot_token"] == "***MASKED***"
    assert d["linha_digitavel"].endswith("026000")
    assert d["other"] == "ok"


def test_pdf_text_and_password_protection(tmp_path: Path) -> None:
    """Extração de PDF e detecção de PDF protegido por senha."""
    img = Image.new("RGB", (200, 80), color="white")
    pdf_path = tmp_path / "encrypted.pdf"
    img.save(pdf_path, "PDF")

    writer = PdfWriter()
    writer.append(str(pdf_path))
    writer.encrypt("segredo123")
    enc_path = tmp_path / "protected.pdf"
    with enc_path.open("wb") as fh:
        writer.write(fh)

    with pytest.raises(PDFPasswordRequiredError):
        extract_text_from_pdf(enc_path)

    # Wrong password raises too
    with pytest.raises(PDFPasswordRequiredError):
        extract_text_from_pdf(enc_path, password="errada")

    # Correct password succeeds without error
    txt = extract_text_from_pdf(enc_path, password="segredo123")
    assert isinstance(txt, str)


def test_ocr_and_llm_privacy_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR helpers + LLM consent & Ollama local fallback."""
    img_path = tmp_path / "boleto.png"
    img_obj = Image.new("RGB", (100, 100), color="white")
    img_obj.save(img_path)
    processed = _preprocess_for_ocr(img_obj)
    assert processed.mode == "L"

    fields = extract_boleto_fields_from_text(
        "Beneficiário: Enel Energia\nVencimento: 25/11/2026\nValor: R$ 156,88"
    )
    assert fields.valor == 15688
    assert fields.vencimento == "2026-11-25"

    # Cloud blocked when consent=False
    monkeypatch.setattr(settings, "cloud_extraction_consent", False)
    assert extract_with_llm_vision(img_path) is None

    # Ollama mocked response
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "response": '{"valor": 15688, "vencimento": "2026-11-25", "beneficiario": "Enel"}'
    }
    fake_client = MagicMock()
    fake_client.__enter__.return_value.post.return_value = fake_resp
    with patch("autobill.extract.llm_vision.httpx.Client", return_value=fake_client):
        res = extract_with_ollama_vision(img_path)
        assert res is not None and res.valor == 15688

    used, limit = get_daily_llm_usage()
    assert limit >= 1 and used >= 0


def test_pipeline_crosscheck_flags_divergence(tmp_path: Path) -> None:
    """RF08: divergência entre valor do código de barras e texto força EM_REVISAO."""
    linha = _build_valid_linha(fator=1400, valor=15688)
    pdf_path = tmp_path / "fatura.pdf"
    Image.new("RGB", (200, 200), color="white").save(pdf_path, "PDF")

    pipeline = ExtractionPipeline(ExtractionConfig(extractor_mode="local", cloud_consent=False))
    fake_text = (
        f"Beneficiário: Enel São Paulo\n"
        f"Linha Digitável: {linha}\n"
        f"Valor do Documento: R$ 999,00\n"
        f"Vencimento: 25/12/2026\n"
    )
    with patch("autobill.extract.pdf_text.extract_text_from_pdf", return_value=fake_text):
        res = pipeline.extract(pdf_path)

    assert res.validation_passed is True
    assert res.valor_confirmado is False
    assert res.needs_review is True
    assert any("Divergência de valor" in d for d in res.discrepancies)


async def test_ingest_bytes_and_folder_and_imap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unified ingestion, deduplication, FolderIngestor stability, and ImapIngestor."""
    from autobill.db.repository import db

    db_path = tmp_path / "ingest.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "auto_confirm", True)
    settings.ensure_directories()

    old_path = db.db_path
    await db.close()
    monkeypatch.setattr(db, "db_path", db_path)
    await db.connect()
    await db.create_tables()

    try:
        # 1. Unsupported extension -> rejected
        rej = await ingest_bytes(
            file_bytes=b"abc", filename="virus.exe", origin=BillOrigin.PASTA, chat_id=500
        )
        assert rej.status == "rejected"

        # 2. Valid PDF extraction via mocked pipeline
        linha = _build_valid_linha(fator=1400, valor=25000)
        fake_res = ExtractionResult(
            valor=25000,
            vencimento="2026-11-20",
            beneficiario="Sabesp",
            linha_digitavel=linha,
            confidence=0.95,
            validation_passed=True,
            valor_confirmado=True,
            needs_review=False,
        )
        with patch("autobill.ingest.processor.ExtractionPipeline.extract", return_value=fake_res):
            out1 = await ingest_bytes(
                file_bytes=b"%PDF-1.4 fake1",
                filename="sabesp.pdf",
                origin=BillOrigin.TELEGRAM,
                chat_id=500,
            )
            assert out1.status == "created"
            assert out1.bill is not None and out1.bill.status == BillStatus.PENDENTE

            # Same hash -> duplicate_hash
            out_dup = await ingest_bytes(
                file_bytes=b"%PDF-1.4 fake1",
                filename="sabesp.pdf",
                origin=BillOrigin.TELEGRAM,
                chat_id=500,
            )
            assert out_dup.status == "duplicate_hash"

            # Different bytes, same linha -> duplicate_linha
            out_dup_l = await ingest_bytes(
                file_bytes=b"%PDF-1.4 fake2",
                filename="sabesp2.pdf",
                origin=BillOrigin.PASTA,
                chat_id=500,
            )
            assert out_dup_l.status == "duplicate_linha"

        # 3. Folder stability & retry queue
        inbox_file = settings.data_dir / "inbox" / "test.txt"
        inbox_file.write_bytes(b"hello")
        assert await wait_for_stable_file(inbox_file, stability_seconds=0.05) is True

        ing = FolderIngestor(bot=None)
        assert ing.inbox_dir.exists()
        assert await process_retry_queue() == 0

        # 4. IMAP ingestor check
        imap_ing = ImapIngestor(bot=None)
        assert hasattr(imap_ing, "poll_once")
    finally:
        await db.close()
        monkeypatch.setattr(db, "db_path", old_path)
