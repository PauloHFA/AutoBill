"""Extração de texto usando LLM Vision (Gemini Flash)."""

import json
import os
from dataclasses import dataclass
from typing import Any

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    genai = None
    types = None


@dataclass
class LLMVisionResult:
    """Resultado da extração via LLM Vision."""
    linha_digitavel: str | None = None
    codigo_barras: str | None = None
    valor: int | None = None  # Em centavos
    vencimento: str | None = None  # ISO YYYY-MM-DD
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
    confidence: float = 0.0
    raw_response: str | None = None


# Prompt para extração de boleto
BOLETO_EXTRACTION_PROMPT = """
Extraia os seguintes campos deste boleto bancário brasileiro.
Responda APENAS com JSON válido, sem texto adicional.

Campos a extrair:
- linha_digitavel: string (47 ou 48 dígitos, apenas números)
- codigo_barras: string (44 dígitos, apenas números)
- valor: integer (valor em centavos, ex: 123456 = R$ 1.234,56)
- vencimento: string (formato ISO YYYY-MM-DD)
- beneficiario: string (nome do beneficiário/cedente)
- pagador: string (nome do pagador/sacado)
- nosso_numero: string
- documento: string (número do documento)
- data_documento: string (formato ISO YYYY-MM-DD)
- especie: string
- aceite: string (S ou N)
- data_processamento: string (formato ISO YYYY-MM-DD)
- instrucoes: string
- local_pagamento: string

Se um campo não for visível/legível, omita-o do JSON
(não inclua null).
Confidence: adicione campo "confidence" com valor 0.0 a 1.0
indicando sua confiança na extração geral.
"""


def get_genai_client() -> Any | None:
    """Retorna cliente GenAI configurado ou None se não disponível."""
    if not GENAI_AVAILABLE:
        return None

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    try:
        return genai.Client(api_key=api_key)
    except Exception:
        return None


def extract_boleto_with_llm_vision(
    image_bytes: bytes,
    mime_type: str = "image/png",
    model: str = "gemini-2.0-flash-exp",
) -> LLMVisionResult | None:
    """
    Extrai campos de boleto usando LLM Vision (Gemini).

    Args:
        image_bytes: Bytes da imagem
        mime_type: Tipo MIME da imagem
        model: Modelo Gemini a usar

    Returns:
        LLMVisionResult com dados extraídos ou None
    """
    client = get_genai_client()
    if not client:
        return None

    try:
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_text(text=BOLETO_EXTRACTION_PROMPT),
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )

        if not response.text:
            return None

        # Parseia resposta JSON
        raw_text = response.text.strip()
        # Remove possíveis marcadores de código
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()

        data = json.loads(raw_text)

        # Extrai confidence
        confidence = data.pop("confidence", 0.0)

        # Converte valor para int se presente
        if "valor" in data and data["valor"] is not None:
            try:
                data["valor"] = int(data["valor"])
            except (ValueError, TypeError):
                data["valor"] = None

        return LLMVisionResult(
            confidence=confidence,
            raw_response=raw_text,
            **data,
        )

    except json.JSONDecodeError:
        return None
    except Exception:
        return None


def is_genai_available() -> bool:
    """Verifica se Google GenAI está disponível e configurado."""
    return GENAI_AVAILABLE and bool(
        os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    )

