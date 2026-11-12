"""Validação específica para arrecadação/concessionária (48 dígitos)."""

from typing import TypedDict

from autobill.rules.modulo import validar_modulo10
from autobill.rules.modulo11 import validar_modulo11

ARRECADACAO_LENGTH = 48
BOLETO_LENGTH = 47


class ArrecadacaoParsed(TypedDict):
    """Estrutura parseada de arrecadação."""
    campo1: str
    campo2: str
    campo3: str
    campo4: str
    dv_geral: str
    campo5: str
    raw: str


def parse_arrecadacao(linha: str) -> ArrecadacaoParsed | None:
    """
    Parseia linha digitável de arrecadação (48 dígitos).

    Estrutura: 11+1 + 11+1 + 11+1 + 1 + 14+1 = 48 dígitos
    Campos: 4 campos de 11 dígitos + DV cada, 1 DV geral, 1 campo de 14 dígitos + DV

    Args:
        linha: Linha digitável (com ou sem formatação)

    Returns:
        Dict com campos parseados ou None se inválida
    """
    limpa = "".join(c for c in linha if c.isdigit())

    if len(limpa) != ARRECADACAO_LENGTH:
        return None

    return ArrecadacaoParsed(
        campo1=limpa[0:11],   # 10 dígitos + DV
        campo2=limpa[11:22],  # 10 dígitos + DV
        campo3=limpa[22:33],  # 10 dígitos + DV
        campo4=limpa[33:44],  # 10 dígitos + DV
        dv_geral=limpa[44],   # DV geral do código de barras
        campo5=limpa[45:48],  # 3 dígitos (parte do valor/fator)
        raw=limpa,
    )


def validar_arrecadacao(linha: str) -> bool:
    """
    Valida linha digitável de arrecadação (48 dígitos).

    Args:
        linha: Linha digitável

    Returns:
        True se válida, False caso contrário
    """
    parsed = parse_arrecadacao(linha)
    if not parsed:
        return False

    # Valida módulo 10 em cada um dos 4 campos
        for campo_key in ("campo1", "campo2", "campo3", "campo4"):
            campo = parsed[campo_key]
        if not validar_modulo10(campo):
            return False

    # Valida módulo 10 no campo5 (se aplicável)
    # Campo5 tem 3 dígitos, pode não ter DV

    # Constrói código de barras para validar módulo 11
    codigo = arrecadacao_para_codigo_barras(parsed)
    if not codigo:
        return False

    return validar_modulo11(codigo)


def arrecadacao_para_codigo_barras(parsed: ArrecadacaoParsed) -> str | None:
    """
    Converte arrecadação parseada para código de barras (44 dígitos).

    Args:
        parsed: Dict retornado por parse_arrecadacao

    Returns:
        Código de barras de 44 dígitos
    """
    # Remove DVs dos 4 campos (cada um tem 10 dígitos + 1 DV)
    campo1 = parsed["campo1"][:10]
    campo2 = parsed["campo2"][:10]
    campo3 = parsed["campo3"][:10]
    campo4 = parsed["campo4"][:10]
    dv_geral = parsed["dv_geral"]
    campo5 = parsed["campo5"]

    return campo1 + campo2 + campo3 + campo4 + dv_geral + campo5.ljust(4, "0")


def identificar_tipo_linha(linha: str) -> str:
    """
    Identifica o tipo de linha digitável.

    Args:
        linha: Linha digitável

    Returns:
        "boleto" (47), "arrecadacao" (48), ou "invalido"
    """
    limpa = "".join(c for c in linha if c.isdigit())

    if len(limpa) == BOLETO_LENGTH:
        return "boleto"
    if len(limpa) == ARRECADACAO_LENGTH:
        return "arrecadacao"
    return "invalido"
