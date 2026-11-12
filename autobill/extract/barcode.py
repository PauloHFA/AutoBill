"""Leitura de código de barras de PDFs e imagens usando zxing-cpp."""

from pathlib import Path

try:
    import zxingcpp
    ZXING_AVAILABLE = True
except ImportError:
    ZXING_AVAILABLE = False
    zxingcpp = None

from PIL import Image

try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    convert_from_path = None


def extract_barcode_from_image(image_path: str | Path) -> str | None:
    """
    Extrai código de barras de uma imagem usando zxing-cpp.

    Args:
        image_path: Caminho para o arquivo de imagem

    Returns:
        Código de barras encontrado (apenas dígitos) ou None
    """
    if not ZXING_AVAILABLE:
        return None

    try:
        image_path = Path(image_path)
        if not image_path.exists():
            return None

        # Abre a imagem com PIL
        with Image.open(image_path) as img_file:
            # Converte para RGB se necessário
            img_rgb = img_file.convert("RGB") if img_file.mode not in ("RGB", "L") else img_file

            # Lê códigos de barras
            results = zxingcpp.read_barcodes(img_rgb)

            for result in results:
                # Filtra apenas códigos numéricos (EAN, Code128, etc.)
                text = result.text.strip()
                # Remove caracteres não numéricos
                digits = "".join(c for c in text if c.isdigit())

                # Códigos de barras de boleto têm 44 dígitos
                # Linha digitável tem 47 ou 48
                if len(digits) in (44, 47, 48):
                    return digits

    except Exception:
        pass

    return None


def extract_barcode_from_pdf(pdf_path: str | Path) -> str | None:
    """
    Extrai código de barras de um PDF.

    Converte cada página para imagem e tenta ler o código de barras.

    Args:
        pdf_path: Caminho para o arquivo PDF

    Returns:
        Código de barras encontrado ou None
    """
    if not ZXING_AVAILABLE or not PDF2IMAGE_AVAILABLE:
        return None

    try:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            return None

        images = convert_from_path(str(pdf_path), dpi=300)

        for page_img in images:
            # Converte para RGB se necessário
            page_img_rgb = (
                page_img.convert("RGB")
                if page_img.mode not in ("RGB", "L")
                else page_img
            )

            results = zxingcpp.read_barcodes(page_img_rgb)

            for result in results:
                text = result.text.strip()
                digits = "".join(c for c in text if c.isdigit())

                if len(digits) in (44, 47, 48):
                    return digits

    except Exception:
        pass

    return None


def extract_all_barcodes_from_image(image_path: str | Path) -> list[str]:
    """
    Extrai todos os códigos de barras de uma imagem.

    Args:
        image_path: Caminho para o arquivo de imagem

    Returns:
        Lista de códigos de barras encontrados
    """
    if not ZXING_AVAILABLE:
        return []

    barcodes = []

    try:
        image_path = Path(image_path)
        if not image_path.exists():
            return []

        with Image.open(image_path) as img_file:
            img_rgb = img_file.convert("RGB") if img_file.mode not in ("RGB", "L") else img_file

            results = zxingcpp.read_barcodes(img_rgb)

            for result in results:
                text = result.text.strip()
                digits = "".join(c for c in text if c.isdigit())
                if len(digits) in (44, 47, 48):
                    barcodes.append(digits)

    except Exception:
        pass

    return barcodes


def extract_all_barcodes_from_pdf(pdf_path: str | Path) -> list[str]:
    """
    Extrai todos os códigos de barras de um PDF.

    Args:
        pdf_path: Caminho para o arquivo PDF

    Returns:
        Lista de códigos de barras encontrados
    """
    if not ZXING_AVAILABLE or not PDF2IMAGE_AVAILABLE:
        return []

    barcodes = []

    try:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            return []

        images = convert_from_path(str(pdf_path), dpi=300)

        for page_img in images:
            page_img_rgb = (
                page_img.convert("RGB")
                if page_img.mode not in ("RGB", "L")
                else page_img
            )

            results = zxingcpp.read_barcodes(page_img_rgb)

            for result in results:
                text = result.text.strip()
                digits = "".join(c for c in text if c.isdigit())
                if len(digits) in (44, 47, 48):
                    barcodes.append(digits)

    except Exception:
        pass

    return barcodes


def is_zxing_available() -> bool:
    """Verifica se zxing-cpp está disponível."""
    return ZXING_AVAILABLE

