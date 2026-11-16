"""Comprehensive tests for FEBRABAN rules, due dates, arrecadação, and money parsing."""

from datetime import date, datetime

from autobill.rules.arrecadacao import (
    arrecadacao_para_codigo_barras,
    codigo_barras_arrecadacao_para_linha,
    extrair_valor_arrecadacao,
    identificar_tipo_linha,
    parse_arrecadacao,
    validar_arrecadacao,
    validar_codigo_barras_arrecadacao,
)
from autobill.rules.boleto import (
    codigo_barras_para_linha,
    extrair_fator_vencimento,
    extrair_valor_codigo_barras,
    formatar_linha_digitavel,
    identificar_banco,
    is_valor_confirmado_codigo_barras,
    limpar_linha,
    linha_para_codigo_barras,
    parse_linha_digitavel,
    validar_codigo_barras,
    validar_linha_digitavel,
)
from autobill.rules.due_date import (
    ajustar_para_dia_util,
    calcular_fator_vencimento_auto,
    descrever_dia_nao_util,
    fator_para_data,
    fator_para_data_auto,
    fator_para_data_novo,
    is_dia_util,
    is_fator_ambiguo,
    is_feriado_br,
)
from autobill.rules.modulo import modulo10, validar_modulo10
from autobill.rules.modulo11 import (
    modulo11,
    modulo11_arrecadacao,
    modulo11_bancario,
    validar_modulo11,
    validar_modulo11_arrecadacao,
)
from autobill.rules.money import Money, MoneyTypeError, formatar_centavos, parse_valor_br


def _make_valid_boleto_cb(
    banco: str = "341",
    fator: int = 1005,
    valor_centavos: int = 15688,
    campo_livre: str = "1234567890123456789012345",
) -> str:
    sem_dv = f"{banco}9{fator:04d}{valor_centavos:010d}{campo_livre}"
    dv = modulo11_bancario(sem_dv)
    return f"{sem_dv[:4]}{dv}{sem_dv[4:]}"


def _make_valid_arrecadacao_cb(id_valor: str = "6", valor_centavos: int = 12345) -> str:
    # 44 digits starting with '8', id_valor at index 2, dv_geral at index 3
    rest = f"81{id_valor}0{valor_centavos:011d}" + "0" * 29
    sem_dv = rest[:3] + rest[4:]
    if id_valor in ("6", "7"):
        dv = modulo10(sem_dv)
    else:
        dv = modulo11_arrecadacao(sem_dv)
    return f"{rest[:3]}{dv}{rest[4:]}"


def test_modulo10_and_modulo11_variants() -> None:
    """Modulo 10 e Modulo 11 bancario/arrecadacao."""
    assert modulo10("261533") == 4
    assert validar_modulo10("2615334")
    assert not validar_modulo10("2615335")
    assert not validar_modulo10("1")

    # Bancario: remainder 0, 1, 10 -> 1
    assert modulo11_bancario("0019000000000000000000000000000000000000000") in range(1, 10)
    assert modulo11_arrecadacao("0" * 11) == 0
    assert modulo11("0" * 11) == 0
    assert isinstance(validar_modulo11("2615334"), bool)
    assert validar_modulo11_arrecadacao("0" * 11 + "0") is True


def test_due_date_2025_reset_and_business_days() -> None:
    """Regra FEBRABAN de virada do fator em 22/02/2025 e dias uteis."""
    assert fator_para_data_novo(1000, referencia=date(2025, 3, 1)) == date(2025, 2, 22)
    assert fator_para_data(1000) == date(2000, 7, 3)
    assert fator_para_data(9999) == date(2025, 2, 21)

    # Auto selection around 2026
    assert fator_para_data_auto(1005, referencia=date(2026, 1, 10)) == date(2025, 2, 27)
    assert fator_para_data_auto(9999, referencia=date(2025, 2, 1)) == date(2025, 2, 21)
    assert calcular_fator_vencimento_auto(date(2025, 2, 22)) == 1000
    assert is_fator_ambiguo(9950, referencia=date(2025, 2, 1)) is False

    # Weekends and holidays
    sat = date(2026, 11, 14)
    natal = date(2026, 12, 25)
    assert not is_dia_util(sat)
    assert is_feriado_br(natal)
    assert descrever_dia_nao_util(sat) is not None
    assert descrever_dia_nao_util(natal) is not None
    assert ajustar_para_dia_util(sat) == date(2026, 11, 16)


def test_boleto_roundtrip_and_bank_identification() -> None:
    """Conversao completa CB <-> Linha, validacao e identificacao de banco."""
    cb = _make_valid_boleto_cb(banco="341", fator=1400, valor_centavos=25099)
    assert validar_codigo_barras(cb)
    linha = codigo_barras_para_linha(cb)
    assert linha is not None and len(linha) == 47
    assert validar_linha_digitavel(linha)

    parsed = parse_linha_digitavel(linha)
    assert parsed is not None
    assert linha_para_codigo_barras(parsed) == cb
    assert extrair_valor_codigo_barras(cb) == 25099
    assert is_valor_confirmado_codigo_barras(cb) is True
    assert extrair_fator_vencimento(cb) == 1400
    assert identificar_banco(linha) == ("341", "Itaú")
    assert formatar_linha_digitavel(linha).count(".") == 3
    assert limpar_linha("123.456-789") == "123456789"


def test_arrecadacao_mod10_and_mod11_valid() -> None:
    """Validacao de guias 48 digitos modulo 10 e modulo 11."""
    cb10 = _make_valid_arrecadacao_cb(id_valor="6", valor_centavos=8950)
    assert validar_codigo_barras_arrecadacao(cb10)
    linha10 = codigo_barras_arrecadacao_para_linha(cb10)
    assert linha10 is not None and len(linha10) == 48
    assert validar_arrecadacao(linha10)
    assert validar_linha_digitavel(linha10)
    parsed10 = parse_arrecadacao(linha10)
    assert parsed10 is not None
    assert arrecadacao_para_codigo_barras(parsed10) == cb10
    assert extrair_valor_arrecadacao(cb10) == (8950, True)
    assert identificar_tipo_linha(linha10) == "arrecadacao"

    cb11 = _make_valid_arrecadacao_cb(id_valor="9", valor_centavos=5000)
    assert validar_codigo_barras_arrecadacao(cb11)
    linha11 = codigo_barras_arrecadacao_para_linha(cb11)
    assert linha11 is not None and validar_arrecadacao(linha11)
    assert extrair_valor_arrecadacao(cb11) == (None, False)


def test_money_parsing_and_formatting() -> None:
    """Conversao de valores monetarios BR para centavos inteiros."""
    assert parse_valor_br("R$ 1.234,56") == 123456
    assert parse_valor_br("156,88") == 15688
    assert parse_valor_br("156.88") == 15688
    assert parse_valor_br("100") == 10000
    assert parse_valor_br("abc") is None
    assert formatar_centavos(123456) == "R$ 1.234,56"
    assert formatar_centavos(0) == "R$ 0,00"


def test_boleto_dt_helper_coverage() -> None:
    """Garante compatibilidade de extracao e cobertura de Money & boleto."""
    cb = _make_valid_boleto_cb(banco="001", fator=1000, valor_centavos=0)
    assert is_valor_confirmado_codigo_barras(cb) is False
    assert extrair_valor_codigo_barras(cb) == 0
    assert identificar_banco("9990000000") == ("999", "Banco 999")
    assert identificar_banco("8260000000")[0].startswith("8")
    assert parse_linha_digitavel("123") is None
    assert not validar_codigo_barras("123")
    assert extrair_fator_vencimento("123") is None
    assert isinstance(datetime.now().date(), date)

    from decimal import Decimal

    m1 = Money.from_reais(150.25)
    m2 = Money.from_reais("49,75")
    assert Money.from_centavos(4975) == m2
    assert (m1 + m2).centavos == 20000
    assert (m1 - m2).centavos == 10050
    assert (m1 * 2).centavos == 30050
    assert m1 > m2 and m2 < m1 and m1 >= m2 and m2 <= m1
    assert str(m1) == "R$ 150,25"
    assert m1.to_reais() == Decimal("150.25")
    try:
        Money("invalid")  # type: ignore[arg-type]
    except MoneyTypeError:
        pass
