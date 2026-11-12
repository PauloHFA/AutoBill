"""Validação e conversão de linha digitável e código de barras."""

from dataclasses import dataclass

from autobill.rules.modulo import modulo10, validar_modulo10
from autobill.rules.modulo11 import validar_modulo11

# Constantes para evitar magic values
BOLETO_LENGTH = 47
ARRECADACAO_LENGTH = 48
CODIGO_BARRAS_LENGTH = 44
CAMPO1_BOLETO_DIGITS = 9
CAMPO2_BOLETO_DIGITS = 10
CAMPO3_BOLETO_DIGITS = 10
CAMPO4_DIGITS = 1
CAMPO5_BOLETO_DIGITS = 14
CAMPO1_ARREC_DIGITS = 10
CAMPO2_ARREC_DIGITS = 10
CAMPO3_ARREC_DIGITS = 10
CAMPO5_ARREC_DIGITS = 13
VALOR_DIGITS = 10
FATOR_DIGITS = 4


@dataclass
class LinhaDigitavel:
    """Estrutura da linha digitável parseada."""
    campo1: str  # 9 dígitos + DV
    campo2: str  # 10 dígitos + DV
    campo3: str  # 10 dígitos + DV
    campo4: str  # 1 dígito (DV geral do código de barras)
    campo5: str  # 14 dígitos (fator vencimento + valor)
    tipo: str    # "boleto" (47) ou "arrecadacao" (48)
    raw: str     # Linha original sem formatação


def parse_linha_digitavel(linha: str) -> LinhaDigitavel | None:
    """
    Parseia uma linha digitável removendo formatação.

    Args:
        linha: Linha digitável com ou sem pontos/espaços

    Returns:
        LinhaDigitavel parseada ou None se inválida
    """
    # Remove formatação
    limpa = "".join(c for c in linha if c.isdigit())

    if len(limpa) == BOLETO_LENGTH:
        # Boleto bancário: 47 dígitos
        return LinhaDigitavel(
            campo1=limpa[0:10],
            campo2=limpa[10:21],
            campo3=limpa[21:32],
            campo4=limpa[32],
            campo5=limpa[33:47],
            tipo="boleto",
            raw=limpa,
        )
    if len(limpa) == ARRECADACAO_LENGTH:
        # Arrecadação/concessionária: 48 dígitos
        return LinhaDigitavel(
            campo1=limpa[0:11],
            campo2=limpa[11:22],
            campo3=limpa[22:33],
            campo4=limpa[33],
            campo5=limpa[34:48],
            tipo="arrecadacao",
            raw=limpa,
        )
    return None


def validar_linha_digitavel(linha: str) -> bool:
    """
    Valida uma linha digitável completa (módulo 10 nos campos + módulo 11 no código de barras).

    Args:
        linha: Linha digitável (com ou sem formatação)

    Returns:
        True se válida, False caso contrário
    """
    parsed = parse_linha_digitavel(linha)
    if not parsed:
        return False

    # Valida módulo 10 em cada campo
    campos_validos = (
        validar_modulo10(parsed.campo1)
        and validar_modulo10(parsed.campo2)
        and validar_modulo10(parsed.campo3)
    )

    if not campos_validos:
        return False

    # Para arrecadação, valida campo5 também
    if parsed.tipo == "arrecadacao" and not validar_modulo10(parsed.campo5):
        return False

    # Valida módulo 11 no código de barras completo
    codigo_barras = linha_para_codigo_barras(parsed)
    if not codigo_barras:
        return False

    return validar_modulo11(codigo_barras)


def linha_para_codigo_barras(parsed: LinhaDigitavel) -> str | None:
    """
    Converte linha digitável para código de barras (44 dígitos).

    Args:
        parsed: LinhaDigitavel parseada

    Returns:
        Código de barras de 44 dígitos ou None se erro
    """
    if parsed.tipo == "boleto":
        # Boleto: remove DVs dos campos 1-3 e reposiciona
        # Campo1: 9 dígitos (sem DV), Campo2: 10 dígitos (sem DV), Campo3: 10 dígitos (sem DV)
        # Campo4: 1 dígito (DV geral), Campo5: 14 dígitos
        # Código = Campo1(9) + Campo2(10) + Campo3(10) + Campo4(1) + Campo5(14) = 44
        campo1_sem_dv = parsed.campo1[:CAMPO1_BOLETO_DIGITS]
        campo2_sem_dv = parsed.campo2[:CAMPO2_BOLETO_DIGITS]
        campo3_sem_dv = parsed.campo3[:CAMPO3_BOLETO_DIGITS]
        return campo1_sem_dv + campo2_sem_dv + campo3_sem_dv + parsed.campo4 + parsed.campo5

    if parsed.tipo == "arrecadacao":
        # Arrecadação: 48 dígitos -> 44 dígitos
        # Remove DVs dos 4 campos (cada um tem 10 ou 11 dígitos + DV)
        campo1_sem_dv = parsed.campo1[:CAMPO1_ARREC_DIGITS]
        campo2_sem_dv = parsed.campo2[:CAMPO2_ARREC_DIGITS]
        campo3_sem_dv = parsed.campo3[:CAMPO3_ARREC_DIGITS]
        campo5_sem_dv = parsed.campo5[:CAMPO5_ARREC_DIGITS]  # 14 dígitos, último é DV
        return campo1_sem_dv + campo2_sem_dv + campo3_sem_dv + parsed.campo4 + campo5_sem_dv

    return None


def codigo_barras_para_linha(codigo: str) -> str | None:
    """
    Converte código de barras (44 dígitos) para linha digitável (47 dígitos).

    Args:
        codigo: Código de barras de 44 dígitos

    Returns:
        Linha digitável de 47 dígitos ou None se erro
    """
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return None

    # Extrai partes do código de barras
    # Posições: 0-8 (9), 9-18 (10), 19-28 (10), 29 (1), 30-43 (14)
    campo1_base = codigo[0:CAMPO1_BOLETO_DIGITS]
    campo2_base = codigo[
        CAMPO1_BOLETO_DIGITS:CAMPO1_BOLETO_DIGITS + CAMPO2_BOLETO_DIGITS
    ]
    campo3_base = codigo[
        CAMPO1_BOLETO_DIGITS + CAMPO2_BOLETO_DIGITS:
        CAMPO1_BOLETO_DIGITS + CAMPO2_BOLETO_DIGITS + CAMPO3_BOLETO_DIGITS
    ]
    campo4 = codigo[
        CAMPO1_BOLETO_DIGITS + CAMPO2_BOLETO_DIGITS + CAMPO3_BOLETO_DIGITS
    ]
    campo5 = codigo[
        CAMPO1_BOLETO_DIGITS + CAMPO2_BOLETO_DIGITS + CAMPO3_BOLETO_DIGITS + CAMPO4_DIGITS:
    ]

    # Calcula DVs módulo 10
    dv1 = modulo10(campo1_base)
    dv2 = modulo10(campo2_base)
    dv3 = modulo10(campo3_base)

    campo1 = campo1_base + str(dv1)
    campo2 = campo2_base + str(dv2)
    campo3 = campo3_base + str(dv3)

    return campo1 + campo2 + campo3 + campo4 + campo5


def validar_codigo_barras(codigo: str) -> bool:
    """
    Valida um código de barras (módulo 11).

    Args:
        codigo: Código de barras de 44 dígitos

    Returns:
        True se válido, False caso contrário
    """
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return False
    return validar_modulo11(codigo)


def extrair_valor_codigo_barras(codigo: str) -> int | None:
    """
    Extrai o valor em centavos do código de barras.

    Args:
        codigo: Código de barras de 44 dígitos

    Returns:
        Valor em centavos ou None se inválido
    """
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return None

    # Valor está nos últimos 10 dígitos do campo5 (posições 34-43 do código)
    # Campo5 = fator vencimento (4) + valor (10)
    valor_str = codigo[CODIGO_BARRAS_LENGTH - VALOR_DIGITS:]
    try:
        return int(valor_str)
    except ValueError:
        return None


def extrair_fator_vencimento(codigo: str) -> int | None:
    """
    Extrai o fator de vencimento do código de barras.

    Args:
        codigo: Código de barras de 44 dígitos

    Returns:
        Fator de vencimento ou None se inválido
    """
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return None

    # Fator está nos primeiros 4 dígitos do campo5 (posições 30-33 do código)
    fator_start = CAMPO1_BOLETO_DIGITS + CAMPO2_BOLETO_DIGITS + CAMPO3_BOLETO_DIGITS + CAMPO4_DIGITS
    fator_str = codigo[fator_start:fator_start + FATOR_DIGITS]
    try:
        return int(fator_str)
    except ValueError:
        return None
