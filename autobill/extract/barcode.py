"""Leitura de código de barras de PDFs e imagens usando zxing-cpp e pypdfium2."""

from importlib import import_module
from pathlib import Path
from typing import Any

from PIL import Image

zxingcpp: Any
try:
    zxingcpp = import_module("zxingcpp")
    ZXING_AVAILABLE = True
except ImportError:
    ZXING_AVAILABLE = False
    zxingcpp = None

pypdfium2: Any
try:
    pypdfium2 = import_module("pypdfium2")
    PYPDFIUM_AVAILABLE = True
except ImportError:
    PYPDFIUM_AVAILABLE = False
    pypdfium2 = None

MAX_PDF_PAGES = 5
VALID_BARCODE_LENGTHS = (44, 47, 48)


def render_pdf_pages_to_images(
    pdf_path: Path,
    *,
    dpi: int = 300,
    max_pages: int = MAX_PDF_PAGES,
    password: str | None = None,
) -> list[Image.Image]:
    """Render up to `max_pages` PDF pages to PIL Images using pypdfium2."""
    if not PYPDFIUM_AVAILABLE:
        return []
    images: list[Image.Image] = []
    doc = pypdfium2.PdfDocument(str(pdf_path), password=password)
    try:
        scale = dpi / 72.0
        for page_index in range(min(len(doc), max_pages)):
            page = doc[page_index]
            try:
                bitmap = page.render(scale=scale)
                images.append(bitmap.to_pil().convert("RGB"))
            finally:
                page.close()
    finally:
        doc.close()
    return images


def _read_barcodes_from_pil(img: Image.Image) -> list[str]:
    found: list[str] = []
    for angle in (0, 90, 180, 270):
        candidate = img if angle == 0 else img.rotate(angle, expand=True)
        img_rgb = candidate.convert("RGB") if candidate.mode not in ("RGB", "L") else candidate
        results = zxingcpp.read_barcodes(img_rgb)
        for result in results:
            digits = "".join(c for c in result.text.strip() if c.isdigit())
            if len(digits) in VALID_BARCODE_LENGTHS and digits not in found:
                found.append(digits)
        if found:
            break
    return found


def extract_barcode_from_image(image_path: str | Path) -> str | None:
    """Extrai código de barras de uma imagem usando zxing-cpp (com tentativa de rotação)."""
    if not ZXING_AVAILABLE:
        return None
    try:
        path = Path(image_path)
        if not path.exists():
            return None
        with Image.open(path) as img_file:
            barcodes = _read_barcodes_from_pil(img_file)
            return barcodes[0] if barcodes else None
    except Exception:
        return None


def extract_barcode_from_pdf(pdf_path: str | Path, *, password: str | None = None) -> str | None:
    """Extrai código de barras de um PDF renderizando até 5 páginas via pypdfium2."""
    if not ZXING_AVAILABLE or not PYPDFIUM_AVAILABLE:
        return None
    try:
        path = Path(pdf_path)
        if not path.exists():
            return None
        for page_img in render_pdf_pages_to_images(path, dpi=300, password=password):
            barcodes = _read_barcodes_from_pil(page_img)
            if barcodes:
                return barcodes[0]
    except Exception:
        return None
    return None


def extract_all_barcodes_from_image(image_path: str | Path) -> list[str]:
    """Extrai todos os códigos de barras de uma imagem."""
    if not ZXING_AVAILABLE:
        return []
    try:
        path = Path(image_path)
        if not path.exists():
            return []
        with Image.open(path) as img_file:
            return _read_barcodes_from_pil(img_file)
    except Exception:
        return []


def extract_all_barcodes_from_pdf(
    pdf_path: str | Path, *, password: str | None = None
) -> list[str]:
    """Extrai todos os códigos de barras de um PDF (até 5 páginas)."""
    if not ZXING_AVAILABLE or not PYPDFIUM_AVAILABLE:
        return []
    barcodes: list[str] = []
    try:
        path = Path(pdf_path)
        if not path.exists():
            return []
        for page_img in render_pdf_pages_to_images(path, dpi=300, password=password):
            for code in _read_barcodes_from_pil(page_img):
                if code not in barcodes:
                    barcodes.append(code)
    except Exception:
        return []
    return barcodes


def is_zxing_available() -> bool:
    """Verifica se zxing-cpp está disponível."""
    return ZXING_AVAILABLE
