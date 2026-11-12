"""Módulo 11 validation for boleto bancário."""

# Constantes para evitar magic values
MODULO11_BASE_PADRAO = 9
MODULO11_DIVISOR = 11
MODULO11_PESO_INICIAL = 2
MODULO11_DV_MAXIMO = 10
MIN_LENGTH_MODULO11 = 2


def modulo11(numero: str, base: int = MODULO11_BASE_PADRAO) -> int:
    """
    Calcula o dígito verificador módulo 11.

    Args:
        numero: String numérica para calcular o DV
        base: Base para o cálculo (padrão 9 para boletos)

    Returns:
        Dígito verificador (0-9, onde 10 vira 0 e 11 vira 1)
    """
    soma = 0
    peso = MODULO11_PESO_INICIAL

    # Processa da direita para a esquerda
    for digito in reversed(numero):
        if not digito.isdigit():
            continue
        soma += int(digito) * peso
        peso += 1
        if peso > base:
            peso = MODULO11_PESO_INICIAL

    resto = soma % MODULO11_DIVISOR
    dv = MODULO11_DIVISOR - resto

    # Regras especiais do módulo 11
    if dv >= MODULO11_DV_MAXIMO:
        dv = 0
    return dv


def validar_modulo11(numero_com_dv: str, base: int = MODULO11_BASE_PADRAO) -> bool:
    """
    Valida um número com dígito verificador módulo 11.

    Args:
        numero_com_dv: Número completo incluindo o DV no final
        base: Base para o cálculo (padrão 9)

    Returns:
        True se válido, False caso contrário
    """
    if len(numero_com_dv) < MIN_LENGTH_MODULO11:
        return False
    numero = numero_com_dv[:-1]
    dv_informado = int(numero_com_dv[-1])
    dv_calculado = modulo11(numero, base)
    return dv_informado == dv_calculado
