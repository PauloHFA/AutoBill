"""Extração de texto usando OCR local (Tesseract) e pypdfium2."""

import io
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from PIL import Image, ImageOps

pytesseract: Any
try:
    pytesseract = import_module("pytesseract")
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False
    pytesseract = None

pypdfium2: Any
try:
    pypdfium2 = import_module("pypdfium2")
    PYPDFIUM_AVAILABLE = True
except ImportError:
    PYPDFIUM_AVAILABLE = False
    pypdfium2 = None

MAX_PDF_PAGES = 5


def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    gray = img.convert("L")
    return ImageOps.autocontrast(gray)


def extract_text_with_ocr(image_path: str | Path, lang: str = "por") -> str:
    """Extrai texto de uma imagem usando Tesseract OCR."""
    if not TESSERACT_AVAILABLE:
        return ""
    try:
        path = Path(image_path)
        if not path.exists():
            return ""
        with Image.open(path) as img_file:
            processed = _preprocess_for_ocr(img_file)
            config = "--psm 6 --oem 3"
            text = cast("str", pytesseract.image_to_string(processed, lang=lang, config=config))
            return text.strip()
    except Exception:
        return ""


def extract_text_with_ocr_from_pdf(
    pdf_path: str | Path,
    lang: str = "por",
    *,
    password: str | None = None,
    max_pages: int = MAX_PDF_PAGES,
) -> str:
    """Extrai texto de até `max_pages` páginas de um PDF usando OCR + pypdfium2."""
    if not TESSERACT_AVAILABLE or not PYPDFIUM_AVAILABLE:
        return ""
    try:
        path = Path(pdf_path)
        if not path.exists():
            return ""
        doc = pypdfium2.PdfDocument(str(path), password=password)
        text_parts: list[str] = []
        try:
            scale = 300 / 72.0
            for idx in range(min(len(doc), max_pages)):
                page = doc[idx]
                try:
                    pil_img = page.render(scale=scale).to_pil()
                    processed = _preprocess_for_ocr(pil_img)
                    config = "--psm 6 --oem 3"
                    text = cast(
                        "str", pytesseract.image_to_string(processed, lang=lang, config=config)
                    )
                    if text.strip():
                        text_parts.append(text.strip())
                finally:
                    page.close()
        finally:
            doc.close()
        return "\n\n".join(text_parts)
    except Exception:
        return ""


def extract_text_with_ocr_from_bytes(image_bytes: bytes, lang: str = "por") -> str:
    """Extrai texto de bytes de imagem usando Tesseract OCR."""
    if not TESSERACT_AVAILABLE:
        return ""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img_bytes:
            processed = _preprocess_for_ocr(img_bytes)
            config = "--psm 6 --oem 3"
            text = cast("str", pytesseract.image_to_string(processed, lang=lang, config=config))
            return text.strip()
    except Exception:
        return ""


def is_tesseract_available() -> bool:
    """Verifica se Tesseract está disponível."""
    if not TESSERACT_AVAILABLE:
        return False
    try:
        version = pytesseract.get_tesseract_version()
    except Exception:
        return False
    return version is not None


def get_tesseract_version() -> str | None:
    """Retorna versão do Tesseract se disponível."""
    if not TESSERACT_AVAILABLE:
        return None
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def configure_tesseract_path(tesseract_cmd: str) -> bool:
    """Configura caminho do executável do Tesseract."""
    if not TESSERACT_AVAILABLE:
        return False
    try:
        if Path(tesseract_cmd).exists():
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            return True
    except Exception:
        return False
    return False
