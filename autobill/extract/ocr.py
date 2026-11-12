"""Extração de texto usando OCR local (Tesseract)."""

import io
from pathlib import Path

try:
    import pytesseract
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False
    pytesseract = None
    Image = None

try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    convert_from_path = None


def extract_text_with_ocr(image_path: str | Path, lang: str = "por") -> str:
    """
    Extrai texto de uma imagem usando Tesseract OCR.

    Args:
        image_path: Caminho para o arquivo de imagem
        lang: Idioma para OCR (padrão: por = português)

    Returns:
        Texto extraído ou string vazia se erro
    """
    if not TESSERACT_AVAILABLE:
        return ""

    try:
        image_path = Path(image_path)
        if not image_path.exists():
            return ""

        with Image.open(image_path) as img_file:
            # Converte para RGB se necessário
            img_rgb = img_file.convert("RGB") if img_file.mode not in ("RGB", "L") else img_file

            # Configurações do Tesseract para melhor precisão
            config = "--psm 6 --oem 3"  # Assume bloco de texto uniforme, LSTM engine

            text = pytesseract.image_to_string(img_rgb, lang=lang, config=config)
            return text.strip()

    except Exception:
        return ""


def extract_text_with_ocr_from_pdf(pdf_path: str | Path, lang: str = "por") -> str:
    """
    Extrai texto de um PDF usando OCR (converte páginas para imagens).

    Args:
        pdf_path: Caminho para o arquivo PDF
        lang: Idioma para OCR

    Returns:
        Texto extraído de todas as páginas
    """
    if not TESSERACT_AVAILABLE or not PDF2IMAGE_AVAILABLE:
        return ""

    try:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            return ""

        images = convert_from_path(str(pdf_path), dpi=300)
        text_parts = []

        for page_img in images:
            page_img_rgb = (
                page_img.convert("RGB")
                if page_img.mode not in ("RGB", "L")
                else page_img
            )

            config = "--psm 6 --oem 3"
            text = pytesseract.image_to_string(page_img_rgb, lang=lang, config=config)
            if text.strip():
                text_parts.append(text.strip())

        return "\n\n".join(text_parts)

    except Exception:
        return ""


def extract_text_with_ocr_from_bytes(image_bytes: bytes, lang: str = "por") -> str:
    """
    Extrai texto de bytes de imagem usando Tesseract OCR.

    Args:
        image_bytes: Bytes da imagem
        lang: Idioma para OCR

    Returns:
        Texto extraído ou string vazia
    """
    if not TESSERACT_AVAILABLE:
        return ""

    try:
        img_bytes = Image.open(io.BytesIO(image_bytes))
        if img_bytes.mode not in ("RGB", "L"):
            img_bytes = img_bytes.convert("RGB")

        config = "--psm 6 --oem 3"
        text = pytesseract.image_to_string(img_bytes, lang=lang, config=config)
        return text.strip()

    except Exception:
        return ""


def is_tesseract_available() -> bool:
    """Verifica se Tesseract está disponível."""
    if not TESSERACT_AVAILABLE:
        return False

    try:
        # Tenta obter versão do tesseract
        version = pytesseract.get_tesseract_version()
    except Exception:
        return False
    else:
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
    """
    Configura caminho do executável do Tesseract.

    Args:
        tesseract_cmd: Caminho completo para o executável tesseract

    Returns:
        True se configurado com sucesso
    """
    if not TESSERACT_AVAILABLE:
        return False

    try:
        if Path(tesseract_cmd).exists():
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        else:
            return False
    except Exception:
        return False
    else:
        return True

