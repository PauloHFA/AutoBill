"""Extração de texto de PDFs e parsing de campos de boleto."""

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from autobill.extract.ocr import extract_text_with_ocr
from autobill.rules.boleto import CODIGO_BARRAS_LENGTH, parse_linha_digitavel
from autobill.rules.money import parse_valor_br

# Constantes
CAMPO5_MIN_LENGTH = 14


@dataclass
class BoletoFields:
    """Campos extraídos de um boleto."""
    linha_digitavel: str | None = None
    codigo_barras: str | None = None
    valor: int | None = None  # em centavos
    vencimento: str | None = None  # ISO format YYYY-MM-DD
    beneficiario: str | None = None
    pagador: str | None = None
    nosso_numero: str | None = None
    documento: str | None = None
    data_documento: str | None = None
    especie: str | None = None
    aceite: str | None = None
    data_processamento: str | None = None
    instrucoes: str | None = None
    local_pagamento: str | None = None


# Padrões regex para extração de campos
PATTERNS = {
    "linha_digitavel": re.compile(
        r"(?:linha\s*digit[aá]vel|c[oó]digo\s*de\s*barras)[\s:]*([\d\s\.\-]{40,})",
        re.IGNORECASE,
    ),
    "codigo_barras": re.compile(
        r"(?:c[oó]digo\s*de\s*barras)[\s:]*([\d\s\.\-]{40,})",
        re.IGNORECASE,
    ),
    "valor": re.compile(
        r"(?:valor\s*(?:do\s*(?:documento|boleto|t[íi]tulo))?|valor\s*total)[\s:]*R?\$?\s*([\d\.,]+)",
        re.IGNORECASE,
    ),
    "vencimento": re.compile(
        r"(?:vencimento|data\s*de\s*vencimento)[\s:]*(\d{2}[\/\-]\d{2}[\/\-]\d{4})",
        re.IGNORECASE,
    ),
    "beneficiario": re.compile(
        r"(?:benefici[aá]rio|cedente|recebedor)[\s:]*([^\n\r]{5,100})",
        re.IGNORECASE,
    ),
    "pagador": re.compile(
        r"(?:pagador|sacado)[\s:]*([^\n\r]{5,100})",
        re.IGNORECASE,
    ),
    "nosso_numero": re.compile(
        r"(?:nosso\s*n[úu]mero|nosso\s*num)[\s:]*([\w\d\.\-]{5,30})",
        re.IGNORECASE,
    ),
    "documento": re.compile(
        r"(?:documento|n[úu]mero\s*do\s*documento)[\s:]*([\w\d\.\-]{5,30})",
        re.IGNORECASE,
    ),
    "data_documento": re.compile(
        r"(?:data\s*do\s*documento|data\s*emiss[aã]o)[\s:]*(\d{2}[\/\-]\d{2}[\/\-]\d{4})",
        re.IGNORECASE,
    ),
    "especie": re.compile(
        r"(?:esp[eé]cie|tipo\s*de\s*t[íi]tulo)[\s:]*([^\n\r]{2,30})",
        re.IGNORECASE,
    ),
    "aceite": re.compile(
        r"(?:aceite)[\s:]*([SNsn])",
        re.IGNORECASE,
    ),
    "data_processamento": re.compile(
        r"(?:data\s*de\s*processamento|processamento)[\s:]*(\d{2}[\/\-]\d{2}[\/\-]\d{4})",
        re.IGNORECASE,
    ),
    "instrucoes": re.compile(
        r"(?:instru[cç][oõ]es|instru[cç][oõ]es\s*de\s*pagamento)[\s:]*([^\n\r]{10,200})",
        re.IGNORECASE,
    ),
    "local_pagamento": re.compile(
        r"(?:local\s*de\s*pagamento|pag[aá]vel\s*em)[\s:]*([^\n\r]{5,100})",
        re.IGNORECASE,
    ),
}


def extract_text_from_pdf(pdf_path: str | Path) -> str:
    """
    Extrai texto de um arquivo PDF.

    Tenta primeiro com pdfplumber (melhor para tabelas/layout),
    cai para pypdf se falhar.

    Args:
        pdf_path: Caminho para o arquivo PDF

    Returns:
        Texto extraído do PDF
    """
    pdf_path = Path(pdf_path)
    text_parts = []

    # Tenta pdfplumber primeiro
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        if text_parts:
            return "\n\n".join(text_parts)
    except Exception:
        pass

    # Fallback para pypdf
    try:
        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n\n".join(text_parts)
    except Exception:
        return ""


def extract_text_from_image(image_path: str | Path) -> str:
    """
    Extrai texto de uma imagem usando OCR.
    Delega para o módulo OCR.

    Args:
        image_path: Caminho para o arquivo de imagem

    Returns:
        Texto extraído da imagem
    """
    return extract_text_with_ocr(image_path)


def clean_extracted_text(text: str) -> str:
    """
    Limpa texto extraído removendo espaços extras e normalizando.

    Args:
        text: Texto bruto extraído

    Returns:
        Texto limpo
    """
    # Normaliza quebras de linha
    text = re.sub(r"\r\n|\r", "\n", text)
    # Remove espaços no início/fim de linhas
    lines = [line.strip() for line in text.split("\n")]
    # Remove linhas vazias duplicadas
    cleaned = []
    prev_empty = False
    for line in lines:
        if line:
            cleaned.append(line)
            prev_empty = False
        elif not prev_empty:
            cleaned.append("")
            prev_empty = True
    return "\n".join(cleaned)


def _clean_field_value(value: str) -> str:
        """Limpa valor de campo extraído."""
        return re.sub(r"\s+", " ", value.strip())


def _process_linha_digitavel(value: str, fields: BoletoFields) -> None:
        """Processa campo linha digitável."""
        value = re.sub(r"[\s\.\-]", "", value)
        if len(value) in (47, 48):
            fields.linha_digitavel = value


def _process_codigo_barras(value: str, fields: BoletoFields) -> None:
        """Processa campo código de barras."""
        value = re.sub(r"[\s\.\-]", "", value)
        if len(value) == CODIGO_BARRAS_LENGTH:
            fields.codigo_barras = value


def _process_valor(value: str, fields: BoletoFields) -> None:
        """Processa campo valor."""
        centavos = parse_valor_br(value)
        if centavos is not None:
            fields.valor = centavos


def _process_date_field(field_name: str, value: str, fields: BoletoFields) -> None:
        """Processa campos de data."""
        normalized = normalize_date(value)
        if field_name == "vencimento":
            fields.vencimento = normalized
        elif field_name == "data_documento":
            fields.data_documento = normalized
        elif field_name == "data_processamento":
            fields.data_processamento = normalized


def _process_aceite(value: str, fields: BoletoFields) -> None:
        """Processa campo aceite."""
        fields.aceite = value.upper()


def _process_generic_field(field_name: str, value: str, fields: BoletoFields) -> None:
        """Processa campos genéricos."""
        setattr(fields, field_name, value)


def _extract_from_linha_digitavel(fields: BoletoFields) -> None:
        """Extrai campos adicionais da linha digitável parseada."""
        if not fields.linha_digitavel:
            return
        parsed = parse_linha_digitavel(fields.linha_digitavel)
        if (
            parsed
            and fields.valor is None
            and parsed.campo5
            and len(parsed.campo5) >= CAMPO5_MIN_LENGTH
        ):
            # Campo5: fator(4) + valor(10) para boleto
            valor_str = parsed.campo5[-10:]
            with contextlib.suppress(ValueError):
                fields.valor = int(valor_str)


def extract_boleto_fields_from_text(text: str) -> BoletoFields:
        """
        Extrai campos de boleto de um texto usando regex.

        Args:
            text: Texto extraído do PDF/imagem

        Returns:
            BoletoFields com os campos encontrados
        """
        text = clean_extracted_text(text)
        fields = BoletoFields()

        # Mapeia processadores por campo
        processors = {
            "linha_digitavel": _process_linha_digitavel,
            "codigo_barras": _process_codigo_barras,
            "valor": _process_valor,
            "vencimento": lambda v, f: _process_date_field("vencimento", v, f),
            "data_documento": lambda v, f: _process_date_field("data_documento", v, f),
            "data_processamento": lambda v, f: _process_date_field("data_processamento", v, f),
            "aceite": _process_aceite,
        }

        # Extrai cada campo usando regex
        for field_name, pattern in PATTERNS.items():
            match = pattern.search(text)
            if not match:
                continue

            value = _clean_field_value(match.group(1))

            processor = processors.get(field_name)
            if processor:
                processor(value, fields)
            else:
                _process_generic_field(field_name, value, fields)

        # Se encontrou linha digitável, parseia para extrair mais campos
        _extract_from_linha_digitavel(fields)

        return fields


def normalize_date(date_str: str) -> str:
    """
    Normaliza data para formato ISO YYYY-MM-DD.

    Args:
        date_str: Data em formato DD/MM/YYYY ou DD-MM-YYYY

    Returns:
        Data em formato ISO ou string original se não conseguir parsear
    """
    # Tenta DD/MM/YYYY
    match = re.match(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", date_str)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return date_str


def extract_boleto_from_pdf(pdf_path: str | Path) -> BoletoFields:
    """
    Extrai dados de boleto de um arquivo PDF.

    Args:
        pdf_path: Caminho para o arquivo PDF

    Returns:
        BoletoFields com os dados extraídos
    """
    text = extract_text_from_pdf(pdf_path)
    return extract_boleto_fields_from_text(text)
