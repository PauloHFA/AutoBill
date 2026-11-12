"""Módulo 10 validation for boleto bancário."""

# Constantes para evitar magic values
MODULO10_BASE = 10
MODULO10_PESO_INICIAL = 2
MODULO10_PESO_ALTERNADO = 3
MIN_LENGTH_MODULO10 = 2


def modulo10(numero: str) -> int:
    """
    Calcula o dígito verificador módulo 10.

    Args:
        numero: String numérica para calcular o DV

    Returns:
        Dígito verificador (0-9)
    """
    soma = 0
    peso = MODULO10_PESO_INICIAL

    # Processa da direita para a esquerda
    for digito in reversed(numero):
        if not digito.isdigit():
            continue
        produto = int(digito) * peso
        soma += produto // 10 + produto % 10
        peso = MODULO10_PESO_ALTERNADO if peso == MODULO10_PESO_INICIAL else MODULO10_PESO_INICIAL

    resto = soma % MODULO10_BASE
    return (MODULO10_BASE - resto) % MODULO10_BASE


def validar_modulo10(numero_com_dv: str) -> bool:
    """
    Valida um número com dígito verificador módulo 10.

    Args:
        numero_com_dv: Número completo incluindo o DV no final

    Returns:
        True se válido, False caso contrário
    """
    if len(numero_com_dv) < MIN_LENGTH_MODULO10:
        return False
    numero = numero_com_dv[:-1]
    dv_informado = int(numero_com_dv[-1])
    dv_calculado = modulo10(numero)
    return dv_informado == dv_calculado
