"""Validação específica para arrecadação/concessionária (FEBRABAN 48 dígitos)."""

from typing import TypedDict

from autobill.rules.modulo import modulo10, validar_modulo10
from autobill.rules.modulo11 import modulo11_arrecadacao, validar_modulo11_arrecadacao

ARRECADACAO_LENGTH = 48
BOLETO_LENGTH = 47
CODIGO_BARRAS_ARREC_LENGTH = 44
BLOCK_LEN_WITH_DV = 12
BLOCK_DATA_LEN = 11

# Identificador de valor efetivo/referência (3º dígito do código)
MODULO10_EFETIVO = "6"
MODULO10_REFERENCIA = "7"
MODULO11_EFETIVO = "8"
MODULO11_REFERENCIA = "9"
VALID_VALUE_INDICATORS = frozenset(
    {MODULO10_EFETIVO, MODULO10_REFERENCIA, MODULO11_EFETIVO, MODULO11_REFERENCIA}
)


class ArrecadacaoParsed(TypedDict):
    """Estrutura parseada de arrecadação (4 blocos de 11 dígitos + 1 DV cada)."""

    campo1: str
    campo2: str
    campo3: str
    campo4: str
    dv_geral: str
    identificador_valor: str
    raw: str


def _limpar_digitos(linha: str) -> str:
    return "".join(c for c in linha if c.isdigit())


def parse_arrecadacao(linha: str) -> ArrecadacaoParsed | None:
    """
    Parseia linha digitável de arrecadação (48 dígitos, iniciando com '8').

    Estrutura FEBRABAN: 4 blocos de 11 dígitos + 1 DV cada = 48 dígitos.
    """
    limpa = _limpar_digitos(linha)
    if len(limpa) != ARRECADACAO_LENGTH or not limpa.startswith("8"):
        return None

    return ArrecadacaoParsed(
        campo1=limpa[0:12],
        campo2=limpa[12:24],
        campo3=limpa[24:36],
        campo4=limpa[36:48],
        dv_geral=limpa[3],
        identificador_valor=limpa[2],
        raw=limpa,
    )


def arrecadacao_para_codigo_barras(parsed: ArrecadacaoParsed) -> str | None:
    """Converte arrecadação parseada (48 dígitos) para código de barras (44 dígitos)."""
    codigo = (
        parsed["campo1"][:BLOCK_DATA_LEN]
        + parsed["campo2"][:BLOCK_DATA_LEN]
        + parsed["campo3"][:BLOCK_DATA_LEN]
        + parsed["campo4"][:BLOCK_DATA_LEN]
    )
    if len(codigo) != CODIGO_BARRAS_ARREC_LENGTH:
        return None
    return codigo


def codigo_barras_arrecadacao_para_linha(codigo: str) -> str | None:
    """Converte código de barras de arrecadação (44 dígitos) para linha digitável (48 dígitos)."""
    if len(codigo) != CODIGO_BARRAS_ARREC_LENGTH or not codigo.isdigit() or codigo[0] != "8":
        return None
    identificador = codigo[2]
    if identificador not in VALID_VALUE_INDICATORS:
        return None

    calc_dv = (
        modulo10
        if identificador in (MODULO10_EFETIVO, MODULO10_REFERENCIA)
        else modulo11_arrecadacao
    )
    blocos = [
        codigo[i : i + BLOCK_DATA_LEN] for i in range(0, CODIGO_BARRAS_ARREC_LENGTH, BLOCK_DATA_LEN)
    ]
    return "".join(f"{b}{calc_dv(b)}" for b in blocos)


def validar_codigo_barras_arrecadacao(codigo: str) -> bool:
    """Valida código de barras de 44 dígitos de arrecadação (módulo 10 ou 11 conforme 3º dígito)."""
    if len(codigo) != CODIGO_BARRAS_ARREC_LENGTH or not codigo.isdigit() or codigo[0] != "8":
        return False

    identificador = codigo[2]
    if identificador not in VALID_VALUE_INDICATORS:
        return False

    dv_geral = int(codigo[3])
    sem_dv = codigo[:3] + codigo[4:]
    if identificador in (MODULO10_EFETIVO, MODULO10_REFERENCIA):
        return dv_geral == modulo10(sem_dv)
    return dv_geral == modulo11_arrecadacao(sem_dv)


def validar_arrecadacao(linha: str) -> bool:
    """
    Valida linha digitável de arrecadação (48 dígitos).

    Verifica início '8', identificador de módulo no 3º dígito (6/7 -> Mod10, 8/9 -> Mod11),
    os 4 DVs de bloco e o DV geral do código de barras.
    """
    parsed = parse_arrecadacao(linha)
    if not parsed:
        return False

    identificador = parsed["identificador_valor"]
    if identificador not in VALID_VALUE_INDICATORS:
        return False

    validador_bloco = (
        validar_modulo10
        if identificador in (MODULO10_EFETIVO, MODULO10_REFERENCIA)
        else validar_modulo11_arrecadacao
    )

    for campo_key in ("campo1", "campo2", "campo3", "campo4"):
        campo = parsed[campo_key]  # type: ignore[literal-required]
        if not validador_bloco(campo):
            return False

    codigo = arrecadacao_para_codigo_barras(parsed)
    if not codigo:
        return False
    return validar_codigo_barras_arrecadacao(codigo)


def extrair_valor_arrecadacao(codigo: str) -> tuple[int | None, bool]:
    """
    Extrai valor de código de barras de arrecadação (44 dígitos).

    Returns:
        (valor_centavos, valor_efetivo_em_reais)
    """
    if len(codigo) != CODIGO_BARRAS_ARREC_LENGTH or not codigo.isdigit() or codigo[0] != "8":
        return (None, False)
    identificador = codigo[2]
    valor_raw = int(codigo[4:15])
    if identificador in (MODULO10_EFETIVO, MODULO11_EFETIVO):
        return (valor_raw, valor_raw > 0)
    # Código de referência / quantidade de moeda não é valor confirmado em reais
    return (None, False)


def identificar_tipo_linha(linha: str) -> str:
    """Identifica o tipo de linha digitável: 'boleto' (47), 'arrecadacao' (48), ou 'invalido'."""
    limpa = _limpar_digitos(linha)
    if len(limpa) == BOLETO_LENGTH:
        return "boleto"
    if len(limpa) == ARRECADACAO_LENGTH and limpa.startswith("8"):
        return "arrecadacao"
    return "invalido"
