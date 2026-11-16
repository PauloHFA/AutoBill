"""Extração de texto de PDFs e parsing de campos de boleto."""

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from autobill.extract.ocr import extract_text_with_ocr
from autobill.rules.boleto import (
    ARRECADACAO_LENGTH,
    BOLETO_LENGTH,
    CODIGO_BARRAS_LENGTH,
    extrair_fator_vencimento,
    extrair_valor_codigo_barras,
    linha_para_codigo_barras,
    parse_linha_digitavel,
    validar_linha_digitavel,
)
from autobill.rules.due_date import fator_para_data_ou_none
from autobill.rules.money import parse_valor_br

# Constantes
MAX_PDF_PAGES = 5


class PDFPasswordRequiredError(Exception):
    """Raised when a PDF is encrypted and requires a password."""


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


# Regex flexíveis para linha digitável em texto livre (47 ou 48 dígitos com separadores)
BOLETO_47_REGEX = re.compile(
    r"(?<!\d)(\d{5}[\.\s]?\d{5}\s+\d{5}[\.\s]?\d{6}\s+\d{5}[\.\s]?\d{6}\s+\d\s+\d{14})(?!\d)"
)
ARREC_48_REGEX = re.compile(
    r"(?<!\d)(8\d{10}[\-\s]?\d[\s\.]+\d{11}[\-\s]?\d[\s\.]+\d{11}[\-\s]?\d[\s\.]+\d{11}[\-\s]?\d)(?!\d)"
)
CONTIGUOUS_47_48_REGEX = re.compile(r"(?<!\d)(\d{47,48})(?!\d)")

# Padrões regex para extração de campos
PATTERNS = {
    "linha_digitavel": re.compile(
        r"(?:linha\s*digit[aá]vel|c[oó]digo\s*de\s*barras)[\s:]*([\d\s\.\-]{44,65})",
        re.IGNORECASE,
    ),
    "codigo_barras": re.compile(
        r"(?:c[oó]digo\s*de\s*barras)[\s:]*([\d\s\.\-]{44,55})",
        re.IGNORECASE,
    ),
    "valor": re.compile(
        r"(?:valor\s*(?:do\s*(?:documento|boleto|t[íi]tulo|cobrado))?|valor\s*total|total\s*a\s*pagar)[\s:=]*R?\$?\s*([\d\.,]+)",
        re.IGNORECASE,
    ),
    "vencimento": re.compile(
        r"(?:vencimento|data\s*de\s*vencimento|vencto\.?)[\s:]*(\d{2}[\/\-]\d{2}[\/\-]\d{4})",
        re.IGNORECASE,
    ),
    "beneficiario": re.compile(
        r"(?:benefici[aá]rio|cedente|recebedor|favorecido|emissor)[\s:]*([^\n]{3,100})",
        re.IGNORECASE,
    ),
    "pagador": re.compile(
        r"(?:pagador|sacado)[\s:]*([^\n]{3,100})",
        re.IGNORECASE,
    ),
    "nosso_numero": re.compile(
        r"(?:nosso\s*n[úu]mero|nosso\s*num\.?)[\s:]*([\w\d\.\-\/]{4,30})",
        re.IGNORECASE,
    ),
    "documento": re.compile(
        r"(?:documento|n[úu]mero\s*do\s*documento|n[º°]\s*doc\.?)[\s:]*([\w\d\.\-\/]{4,30})",
        re.IGNORECASE,
    ),
    "data_documento": re.compile(
        r"(?:data\s*do\s*documento|data\s*emiss[aã]o)[\s:]*(\d{2}[\/\-]\d{2}[\/\-]\d{4})",
        re.IGNORECASE,
    ),
    "especie": re.compile(
        r"(?:esp[eé]cie|tipo\s*de\s*t[íi]tulo)[\s:]*([^\n]{2,30})",
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
        r"(?:instru[cç][oõ]es|instru[cç][oõ]es\s*de\s*pagamento)[\s:]*([^\n]{10,200})",
        re.IGNORECASE,
    ),
    "local_pagamento": re.compile(
        r"(?:local\s*de\s*pagamento|pag[aá]vel\s*em)[\s:]*([^\n]{5,100})",
        re.IGNORECASE,
    ),
}


def extract_text_from_pdf(
    pdf_path: str | Path,
    *,
    password: str | None = None,
    max_pages: int = MAX_PDF_PAGES,
) -> str:
    """
    Extrai texto de um arquivo PDF (até `max_pages` páginas).

    Detecta PDFs protegidos por senha e dispara `PDFPasswordRequiredError` quando
    a senha não foi fornecida ou está incorreta.
    """
    pdf_path = Path(pdf_path)

    # Verifica criptografia primeiro via pypdf
    try:
        reader = PdfReader(str(pdf_path))
        if reader.is_encrypted:
            if not password:
                raise PDFPasswordRequiredError("PDF protegido por senha")
            decrypt_status = reader.decrypt(password)
            if decrypt_status == 0:
                raise PDFPasswordRequiredError("Senha do PDF incorreta")
            pages = reader.pages[:max_pages]
            return "\n\n".join(p.extract_text() or "" for p in pages).strip()
    except PDFPasswordRequiredError:
        raise
    except Exception:
        pass

    text_parts: list[str] = []

    # Tenta pdfplumber primeiro (melhor para layout tabular de boletos)
    try:
        with pdfplumber.open(pdf_path, password=password) as pdf:
            for page in pdf.pages[:max_pages]:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        if text_parts:
            return "\n\n".join(text_parts)
    except Exception as exc:
        if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
            raise PDFPasswordRequiredError("PDF protegido por senha") from exc

    # Fallback para pypdf
    try:
        reader = PdfReader(str(pdf_path))
        for pdf_page in reader.pages[:max_pages]:
            page_text = pdf_page.extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n\n".join(text_parts)
    except Exception:
        return ""


def extract_text_from_image(image_path: str | Path) -> str:
    """Extrai texto de uma imagem usando OCR."""
    return extract_text_with_ocr(image_path)


def clean_extracted_text(text: str) -> str:
    """Limpa texto extraído removendo espaços extras e normalizando."""
    text = text.replace(chr(13) + "\n", "\n").replace(chr(13), "\n")
    lines = [line.strip() for line in text.split("\n")]
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
    return re.sub(r"\s+", " ", value.strip())


def _find_best_linha_digitavel(text: str) -> str | None:
    """Procura candidatos de linha digitável (47 ou 48 dígitos) com e sem rótulo."""
    candidates: list[str] = []
    for regex in (BOLETO_47_REGEX, ARREC_48_REGEX, CONTIGUOUS_47_48_REGEX):
        for match in regex.finditer(text):
            digits = "".join(c for c in match.group(1) if c.isdigit())
            if len(digits) in (BOLETO_LENGTH, ARRECADACAO_LENGTH):
                candidates.append(digits)

    # Prioriza candidato matematicamente válido
    for cand in candidates:
        if validar_linha_digitavel(cand):
            return cand
    return candidates[0] if candidates else None


def _process_linha_digitavel(value: str, fields: BoletoFields) -> None:
    digits = re.sub(r"[\s\.\-]", "", value)
    if len(digits) in (BOLETO_LENGTH, ARRECADACAO_LENGTH):
        fields.linha_digitavel = digits


def _process_codigo_barras(value: str, fields: BoletoFields) -> None:
    digits = re.sub(r"[\s\.\-]", "", value)
    if len(digits) == CODIGO_BARRAS_LENGTH:
        fields.codigo_barras = digits


def _process_valor(value: str, fields: BoletoFields) -> None:
    centavos = parse_valor_br(value)
    if centavos is not None:
        fields.valor = centavos


def _process_date_field(field_name: str, value: str, fields: BoletoFields) -> None:
    normalized = normalize_date(value)
    if field_name == "vencimento":
        fields.vencimento = normalized
    elif field_name == "data_documento":
        fields.data_documento = normalized
    elif field_name == "data_processamento":
        fields.data_processamento = normalized


def _process_aceite(value: str, fields: BoletoFields) -> None:
    fields.aceite = value.upper()


def _process_generic_field(field_name: str, value: str, fields: BoletoFields) -> None:
    setattr(fields, field_name, value)


def _extract_from_linha_digitavel(fields: BoletoFields) -> None:
    """Extrai código de barras, valor e vencimento de fallback a partir da linha digitável."""
    if not fields.linha_digitavel:
        return
    parsed = parse_linha_digitavel(fields.linha_digitavel)
    if not parsed:
        return
    codigo = linha_para_codigo_barras(parsed)
    if codigo:
        if not fields.codigo_barras:
            fields.codigo_barras = codigo
        if fields.valor is None:
            with contextlib.suppress(Exception):
                val = extrair_valor_codigo_barras(codigo)
                if val and val > 0:
                    fields.valor = val
        if fields.vencimento is None:
            fator = extrair_fator_vencimento(codigo)
            if fator:
                due = fator_para_data_ou_none(fator)
                if due:
                    fields.vencimento = due.isoformat()


def extract_boleto_fields_from_text(text: str) -> BoletoFields:
    """Extrai campos de boleto de um texto usando regex e parsing FEBRABAN."""
    text = clean_extracted_text(text)
    fields = BoletoFields()

    processors = {
        "linha_digitavel": _process_linha_digitavel,
        "codigo_barras": _process_codigo_barras,
        "valor": _process_valor,
        "vencimento": lambda v, f: _process_date_field("vencimento", v, f),
        "data_documento": lambda v, f: _process_date_field("data_documento", v, f),
        "data_processamento": lambda v, f: _process_date_field("data_processamento", v, f),
        "aceite": _process_aceite,
    }

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

    # Fallback para linha digitável sem rótulo explícito no texto
    if not fields.linha_digitavel or not validar_linha_digitavel(fields.linha_digitavel):
        free_match = _find_best_linha_digitavel(text)
        if free_match:
            fields.linha_digitavel = free_match

    _extract_from_linha_digitavel(fields)
    return fields


def normalize_date(date_str: str) -> str:
    """Normaliza data para formato ISO YYYY-MM-DD."""
    match = re.match(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", date_str)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return date_str


def extract_boleto_from_pdf(
    pdf_path: str | Path,
    *,
    password: str | None = None,
) -> BoletoFields:
    """Extrai dados de boleto de um arquivo PDF."""
    text = extract_text_from_pdf(pdf_path, password=password)
    return extract_boleto_fields_from_text(text)
