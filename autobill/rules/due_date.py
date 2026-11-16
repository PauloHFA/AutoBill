"""Cálculo de fator de vencimento, dias úteis e feriados brasileiros."""

from datetime import date, timedelta
from functools import lru_cache

import holidays

# Data base para fator de vencimento: 07/10/1997 (ciclo 1: até 21/02/2025 = fator 9999)
DATA_BASE_FATOR = date(1997, 10, 7)

# Data de reinício do fator de vencimento FEBRABAN: 22/02/2025 -> fator 1000
DATA_MUDANCA_2025 = date(2025, 2, 22)
FATOR_REINICIO_2025 = 1000
FATOR_MAXIMO = 9999

# Constantes para dias da semana
SABADO = 5
DOMINGO = 6
DIAS_AJUSTE_SABADO = 2
DIAS_AJUSTE_DOMINGO = 1
JANELA_AMBIGUIDADE_DIAS = 365 * 2


@lru_cache(maxsize=16)
def _feriados_br(year: int) -> holidays.HolidayBase:
    return holidays.country_holidays("BR", years=[year])


def calcular_fator_vencimento(data_vencimento: date) -> int:
    """Calcula o fator de vencimento clássico (dias desde 07/10/1997)."""
    delta = data_vencimento - DATA_BASE_FATOR
    return delta.days


def fator_para_data(fator: int) -> date:
    """Converte fator de vencimento clássico para data (base 07/10/1997)."""
    return DATA_BASE_FATOR + timedelta(days=fator)


def calcular_fator_vencimento_novo(data_vencimento: date) -> int:
    """
    Calcula fator de vencimento considerando o reinício FEBRABAN de 22/02/2025.

    - Datas anteriores a 22/02/2025: dias desde 07/10/1997 (até 9999 em 21/02/2025).
    - A partir de 22/02/2025: fator = 1000 + dias desde 22/02/2025.
    """
    if data_vencimento < DATA_MUDANCA_2025:
        return calcular_fator_vencimento(data_vencimento)

    delta = data_vencimento - DATA_MUDANCA_2025
    return FATOR_REINICIO_2025 + delta.days


def fator_para_data_candidatos(fator: int) -> tuple[date, date | None]:
    """
    Retorna as datas candidatas para um fator (ciclo antigo 1997, ciclo novo 2025+).
    """
    data_antiga = fator_para_data(fator)
    if FATOR_REINICIO_2025 <= fator <= FATOR_MAXIMO:
        data_nova = DATA_MUDANCA_2025 + timedelta(days=fator - FATOR_REINICIO_2025)
        return (data_antiga, data_nova)
    return (data_antiga, None)


def fator_para_data_novo(fator: int, referencia: date | None = None) -> date:
    """
    Converte fator de vencimento considerando o reinício de 22/02/2025 (fator 1000).

    Para fatores entre 1000 e 9999, quando `referencia` (ou hoje) for >= 22/02/2025,
    seleciona o ciclo (1997 ou 2025) mais próximo da data de referência.
    """
    if fator < FATOR_REINICIO_2025 or fator > FATOR_MAXIMO:
        return fator_para_data(fator)

    ref = referencia or date.today()
    data_antiga, data_nova = fator_para_data_candidatos(fator)
    assert data_nova is not None

    if ref < DATA_MUDANCA_2025:
        return data_antiga

    if abs((data_nova - ref).days) <= abs((data_antiga - ref).days):
        return data_nova
    return data_antiga


def fator_para_data_ou_none(fator: int, referencia: date | None = None) -> date | None:
    """Converte fator para data, retornando None quando o fator for 0 (sem vencimento)."""
    if fator <= 0:
        return None
    return fator_para_data_novo(fator, referencia=referencia)


def is_fator_ambiguo(fator: int, referencia: date | None = None) -> bool:
    """
    Indica se o fator pode representar tanto uma data do ciclo antigo quanto do ciclo novo
    próximas à data de referência (ex.: fatores 9000–9999 perto de 2025/2026).
    """
    if not (FATOR_REINICIO_2025 <= fator <= FATOR_MAXIMO):
        return False
    ref = referencia or date.today()
    data_antiga, data_nova = fator_para_data_candidatos(fator)
    if data_nova is None:
        return False
    return (
        abs((data_antiga - ref).days) <= JANELA_AMBIGUIDADE_DIAS
        and abs((data_nova - ref).days) <= JANELA_AMBIGUIDADE_DIAS
    )


def calcular_fator_vencimento_auto(data_vencimento: date) -> int:
    """Calcula fator de vencimento automaticamente (usa regra vigente)."""
    return calcular_fator_vencimento_novo(data_vencimento)


def fator_para_data_auto(fator: int, referencia: date | None = None) -> date:
    """Converte fator para data automaticamente (usa regra vigente)."""
    return fator_para_data_novo(fator, referencia=referencia)


def validar_fator_vencimento(fator: int, data_vencimento: date) -> bool:
    """Valida se o fator corresponde à data (considera ambos os ciclos 1997 e 2025)."""
    if calcular_fator_vencimento(data_vencimento) == fator:
        return True
    return calcular_fator_vencimento_novo(data_vencimento) == fator


def is_feriado_br(data: date) -> bool:
    """Verifica se a data é feriado nacional brasileiro."""
    return data in _feriados_br(data.year)


def is_dia_util(data: date) -> bool:
    """Verifica se a data é dia útil (não fim de semana nem feriado nacional BR)."""
    return data.weekday() < SABADO and not is_feriado_br(data)


def ajustar_para_dia_util(data: date) -> date:
    """Ajusta data para o próximo dia útil (pulando fins de semana e feriados BR)."""
    atual = data
    while not is_dia_util(atual):
        atual += timedelta(days=1)
    return atual


def dia_util_anterior(data: date) -> date:
    """Retorna o dia útil imediatamente anterior (ex.: sexta se vence sábado/domingo)."""
    atual = data
    while not is_dia_util(atual):
        atual -= timedelta(days=1)
    return atual


def descrever_dia_nao_util(data: date) -> str | None:
    """Retorna aviso legível quando o vencimento cai em fim de semana ou feriado nacional."""
    if is_dia_util(data):
        return None
    if data.weekday() == SABADO:
        sexta = dia_util_anterior(data)
        return f"vence em sábado; pague na sexta ({sexta.strftime('%d/%m')}) ou próximo dia útil"
    if data.weekday() == DOMINGO:
        sexta = dia_util_anterior(data)
        return f"vence em domingo; pague na sexta ({sexta.strftime('%d/%m')}) ou próximo dia útil"
    nome_feriado = _feriados_br(data.year).get(data, "feriado")
    proximo = ajustar_para_dia_util(data)
    return f"vence em feriado ({nome_feriado}); próximo dia útil: {proximo.strftime('%d/%m')}"
