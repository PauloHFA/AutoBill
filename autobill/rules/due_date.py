"""Cálculo de fator de vencimento e datas."""

from datetime import date, timedelta

# Data base para fator de vencimento: 07/10/1997
DATA_BASE_FATOR = date(1997, 10, 7)

# Data de mudança da regra (fevereiro 2025)
DATA_MUDANCA_2025 = date(2025, 2, 22)

# Constantes para dias da semana
SABADO = 5
DOMINGO = 6

# Constantes para fator de vencimento
FATOR_LIMITE_NOVA_REGRA = 9000
DIAS_AJUSTE_SABADO = 2
DIAS_AJUSTE_DOMINGO = 1


def calcular_fator_vencimento(data_vencimento: date) -> int:
    """
    Calcula o fator de vencimento (dias desde 07/10/1997).

    Args:
        data_vencimento: Data de vencimento

    Returns:
        Fator de vencimento (número de dias)
    """
    delta = data_vencimento - DATA_BASE_FATOR
    return delta.days


def fator_para_data(fator: int) -> date:
    """
    Converte fator de vencimento para data.

    Args:
        fator: Fator de vencimento

    Returns:
        Data de vencimento correspondente
    """
    return DATA_BASE_FATOR + timedelta(days=fator)


def calcular_fator_vencimento_novo(data_vencimento: date) -> int:
    """
    Calcula fator de vencimento pela nova regra (pós-fevereiro 2025).

    A partir de 22/02/2025, fatores acima de 9000 usam nova base.
    Nova base: 22/02/2025 = fator 9000

    Args:
        data_vencimento: Data de vencimento

    Returns:
        Fator de vencimento pela nova regra
    """
    if data_vencimento <= DATA_MUDANCA_2025:
        return calcular_fator_vencimento(data_vencimento)

    # Nova regra: fator = 9000 + dias desde 22/02/2025
    delta = data_vencimento - DATA_MUDANCA_2025
    return FATOR_LIMITE_NOVA_REGRA + delta.days


def fator_para_data_novo(fator: int) -> date:
    """
    Converte fator de vencimento (nova regra) para data.

    Args:
        fator: Fator de vencimento (pode ser > 9000)

    Returns:
        Data de vencimento correspondente
    """
    if fator <= FATOR_LIMITE_NOVA_REGRA:
        return fator_para_data(fator)

    # Nova regra: fator = 9000 + dias desde 22/02/2025
    dias_desde_mudanca = fator - FATOR_LIMITE_NOVA_REGRA
    return DATA_MUDANCA_2025 + timedelta(days=dias_desde_mudanca)


def calcular_fator_vencimento_auto(data_vencimento: date) -> int:
    """
    Calcula fator de vencimento automaticamente (usa regra apropriada).

    Args:
        data_vencimento: Data de vencimento

    Returns:
        Fator de vencimento
    """
    return calcular_fator_vencimento_novo(data_vencimento)


def fator_para_data_auto(fator: int) -> date:
    """
    Converte fator para data automaticamente (usa regra apropriada).

    Args:
        fator: Fator de vencimento

    Returns:
        Data de vencimento
    """
    return fator_para_data_novo(fator)


def validar_fator_vencimento(fator: int, data_vencimento: date) -> bool:
    """
    Valida se o fator corresponde à data (considera ambas as regras).

    Args:
        fator: Fator de vencimento
        data_vencimento: Data de vencimento

    Returns:
        True se compatível, False caso contrário
    """
    # Tenta regra antiga
    if calcular_fator_vencimento(data_vencimento) == fator:
        return True

    # Tenta regra nova
    return calcular_fator_vencimento_novo(data_vencimento) == fator


def ajustar_para_dia_util(data: date) -> date:
    """
    Ajusta data para o próximo dia útil se cair em fim de semana.

    Args:
        data: Data original

    Returns:
        Data ajustada para dia útil
    """
    if data.weekday() == SABADO:
        return data + timedelta(days=DIAS_AJUSTE_SABADO)
    if data.weekday() == DOMINGO:
        return data + timedelta(days=DIAS_AJUSTE_DOMINGO)
    return data


def is_dia_util(data: date) -> bool:
    """Verifica se a data é dia útil (não fim de semana)."""
    return data.weekday() < SABADO
