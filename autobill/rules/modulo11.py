"""Módulo 11 validation for boleto bancário."""

# Constantes para evitar magic values
MODULO11_BASE_PADRAO = 9
MODULO11_DIVISOR = 11
MODULO11_PESO_INICIAL = 2
MODULO11_DV_MAXIMO = 10
MIN_LENGTH_MODULO11 = 2


def _soma_ponderada_modulo11(numero: str, base: int = MODULO11_BASE_PADRAO) -> int:
    soma = 0
    peso = MODULO11_PESO_INICIAL
    for digito in reversed(numero):
        if not digito.isdigit():
            continue
        soma += int(digito) * peso
        peso += 1
        if peso > base:
            peso = MODULO11_PESO_INICIAL
    return soma


def modulo11(numero: str, base: int = MODULO11_BASE_PADRAO) -> int:
    """
    Calcula o dígito verificador módulo 11 genérico.

    Args:
        numero: String numérica para calcular o DV
        base: Base para o cálculo (padrão 9 para boletos)

    Returns:
        Dígito verificador (0-9, onde 10 e 11 viram 0)
    """
    soma = _soma_ponderada_modulo11(numero, base)
    resto = soma % MODULO11_DIVISOR
    dv = MODULO11_DIVISOR - resto

    if dv >= MODULO11_DV_MAXIMO:
        dv = 0
    return dv


def modulo11_bancario(numero: str, base: int = MODULO11_BASE_PADRAO) -> int:
    """
    Calcula o DAC módulo 11 para código de barras de boleto bancário (FEBRABAN).

    Regra FEBRABAN: se o resultado (11 - resto) for 0, 10 ou 11, o DAC é 1.
    """
    soma = _soma_ponderada_modulo11(numero, base)
    resto = soma % MODULO11_DIVISOR
    dv = MODULO11_DIVISOR - resto
    if dv in (0, 10, 11):
        return 1
    return dv


def modulo11_arrecadacao(numero: str, base: int = MODULO11_BASE_PADRAO) -> int:
    """
    Calcula o DAC módulo 11 para guias de arrecadação/concessionárias (FEBRABAN).

    Regra FEBRABAN:
    - resto 0 ou 1 -> DAC = 0
    - resto 10 -> DAC = 1
    - demais -> DAC = 11 - resto
    """
    soma = _soma_ponderada_modulo11(numero, base)
    resto = soma % MODULO11_DIVISOR
    if resto in (0, 1):
        return 0
    if resto == MODULO11_DV_MAXIMO:
        return 1
    return MODULO11_DIVISOR - resto


def validar_modulo11(numero_com_dv: str, base: int = MODULO11_BASE_PADRAO) -> bool:
    """
    Valida um número com dígito verificador módulo 11 genérico (DV no último caractere).
    """
    if len(numero_com_dv) < MIN_LENGTH_MODULO11 or not numero_com_dv.isdigit():
        return False
    numero = numero_com_dv[:-1]
    dv_informado = int(numero_com_dv[-1])
    dv_calculado = modulo11(numero, base)
    return dv_informado == dv_calculado


def validar_modulo11_arrecadacao(numero_com_dv: str, base: int = MODULO11_BASE_PADRAO) -> bool:
    """Valida um bloco de arrecadação com DV módulo 11 no último caractere."""
    if len(numero_com_dv) < MIN_LENGTH_MODULO11 or not numero_com_dv.isdigit():
        return False
    numero = numero_com_dv[:-1]
    dv_informado = int(numero_com_dv[-1])
    return dv_informado == modulo11_arrecadacao(numero, base)
