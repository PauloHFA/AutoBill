"""Validação e conversão FEBRABAN de linha digitável e código de barras."""

from dataclasses import dataclass

from autobill.rules.arrecadacao import (
    codigo_barras_arrecadacao_para_linha,
    extrair_valor_arrecadacao,
    validar_arrecadacao,
    validar_codigo_barras_arrecadacao,
)
from autobill.rules.modulo import modulo10, validar_modulo10
from autobill.rules.modulo11 import modulo11_bancario

# Constantes para evitar magic values
BOLETO_LENGTH = 47
ARRECADACAO_LENGTH = 48
CODIGO_BARRAS_LENGTH = 44
CAMPO1_BOLETO_DIGITS = 9
CAMPO2_BOLETO_DIGITS = 10
CAMPO3_BOLETO_DIGITS = 10
CAMPO4_DIGITS = 1
CAMPO5_BOLETO_DIGITS = 14
ARREC_BLOCK_DATA_DIGITS = 11
VALOR_DIGITS = 10
FATOR_DIGITS = 4

BANCOS_FEBRABAN: dict[str, str] = {
    "001": "Banco do Brasil",
    "033": "Santander",
    "041": "Banrisul",
    "070": "BRB",
    "077": "Banco Inter",
    "104": "Caixa Econômica Federal",
    "136": "Unicred",
    "197": "Stone",
    "208": "BTG Pactual",
    "212": "Banco Original",
    "237": "Bradesco",
    "260": "Nubank",
    "290": "PagBank",
    "318": "BMG",
    "323": "Mercado Pago",
    "336": "C6 Bank",
    "341": "Itaú",
    "380": "PicPay",
    "403": "Cora",
    "422": "Banco Safra",
    "655": "Banco Votorantim / Neon",
    "707": "Banco Daycoval",
    "745": "Citibank",
    "748": "Sicredi",
    "756": "Sicoob",
}

SEGMENTOS_ARRECADACAO: dict[str, str] = {
    "1": "Prefeituras",
    "2": "Saneamento",
    "3": "Energia Elétrica e Gás",
    "4": "Telecomunicações",
    "5": "Órgãos Governamentais",
    "6": "Carnes e Assemelhados",
    "7": "Multas de Trânsito",
    "9": "Uso Exclusivo do Banco",
}


@dataclass
class LinhaDigitavel:
    """Estrutura da linha digitável parseada."""

    campo1: str  # Boleto: 9+DV | Arrecadação: 11+DV
    campo2: str  # Boleto: 10+DV | Arrecadação: 11+DV
    campo3: str  # Boleto: 10+DV | Arrecadação: 11+DV
    campo4: str  # Boleto: DV geral (1) | Arrecadação: 11+DV
    campo5: str  # Boleto: fator(4)+valor(10) | Arrecadação: ""
    tipo: str  # "boleto" (47) ou "arrecadacao" (48)
    raw: str  # Linha original apenas com dígitos


def limpar_linha(linha: str) -> str | None:
    """
    Remove espaços, pontos e hífens. Retorna None se houver outros caracteres inválidos.
    """
    if not linha:
        return None
    limpa = []
    for char in linha.strip():
        if char.isdigit():
            limpa.append(char)
        elif char in ".-" or char.isspace():
            continue
        else:
            return None
    return "".join(limpa)


def parse_linha_digitavel(linha: str) -> LinhaDigitavel | None:
    """
    Parseia uma linha digitável removendo formatação (espaços, pontos, hífens).
    Rejeita se houver caracteres não permitidos ou tamanho diferente de 47/48.
    """
    limpa = limpar_linha(linha)
    if not limpa:
        return None

    if len(limpa) == BOLETO_LENGTH:
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
        return LinhaDigitavel(
            campo1=limpa[0:12],
            campo2=limpa[12:24],
            campo3=limpa[24:36],
            campo4=limpa[36:48],
            campo5="",
            tipo="arrecadacao",
            raw=limpa,
        )
    return None


def validar_linha_digitavel(linha: str) -> bool:
    """
    Valida uma linha digitável completa conforme padrão FEBRABAN:
    - Boleto (47): módulo 10 nos campos 1-3 + módulo 11 bancário no código de barras.
    - Arrecadação (48, início '8'): módulo 10 ou 11 nos 4 blocos e no DV geral.
    """
    parsed = parse_linha_digitavel(linha)
    if not parsed:
        return False

    if parsed.tipo == "arrecadacao":
        return validar_arrecadacao(parsed.raw)

    campos_validos = (
        validar_modulo10(parsed.campo1)
        and validar_modulo10(parsed.campo2)
        and validar_modulo10(parsed.campo3)
    )
    if not campos_validos:
        return False

    codigo_barras = linha_para_codigo_barras(parsed)
    if not codigo_barras:
        return False

    return validar_codigo_barras(codigo_barras)


def linha_para_codigo_barras(parsed: LinhaDigitavel) -> str | None:
    """
    Converte linha digitável parseada para código de barras (44 dígitos) padrão FEBRABAN.

    Boleto bancário (47 -> 44):
      pos 0..3   : banco(3) + moeda(1) = campo1[0:4]
      pos 4      : DV geral            = campo4
      pos 5..18  : fator(4)+valor(10)  = campo5
      pos 19..23 : campo livre (5)     = campo1[4:9]
      pos 24..33 : campo livre (10)    = campo2[0:10]
      pos 34..43 : campo livre (10)    = campo3[0:10]
    """
    if parsed.tipo == "boleto":
        return (
            parsed.campo1[0:4]
            + parsed.campo4
            + parsed.campo5
            + parsed.campo1[4:9]
            + parsed.campo2[0:10]
            + parsed.campo3[0:10]
        )

    if parsed.tipo == "arrecadacao":
        return (
            parsed.campo1[:ARREC_BLOCK_DATA_DIGITS]
            + parsed.campo2[:ARREC_BLOCK_DATA_DIGITS]
            + parsed.campo3[:ARREC_BLOCK_DATA_DIGITS]
            + parsed.campo4[:ARREC_BLOCK_DATA_DIGITS]
        )

    return None


def codigo_barras_para_linha(codigo: str) -> str | None:
    """
    Converte código de barras (44 dígitos) para linha digitável (47 ou 48 dígitos).
    """
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return None

    if codigo[0] == "8":
        return codigo_barras_arrecadacao_para_linha(codigo)

    campo1_base = codigo[0:4] + codigo[19:24]
    campo2_base = codigo[24:34]
    campo3_base = codigo[34:44]
    campo4 = codigo[4]
    campo5 = codigo[5:19]

    campo1 = campo1_base + str(modulo10(campo1_base))
    campo2 = campo2_base + str(modulo10(campo2_base))
    campo3 = campo3_base + str(modulo10(campo3_base))

    return campo1 + campo2 + campo3 + campo4 + campo5


def validar_codigo_barras(codigo: str) -> bool:
    """Valida um código de barras de 44 dígitos (bancário ou arrecadação)."""
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return False
    if codigo[0] == "8":
        return validar_codigo_barras_arrecadacao(codigo)

    dv_informado = int(codigo[4])
    sem_dv = codigo[:4] + codigo[5:]
    return dv_informado == modulo11_bancario(sem_dv)


def extrair_valor_codigo_barras(codigo: str) -> int | None:
    """
    Extrai o valor em centavos do código de barras (44 dígitos).

    Para boleto bancário, lê posições 9..18 (10 dígitos). Valor 0 significa sem valor fixo.
    Para arrecadação com identificador 6 ou 8, lê posições 4..14 (11 dígitos).
    """
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return None

    if codigo[0] == "8":
        valor, _efetivo = extrair_valor_arrecadacao(codigo)
        return valor

    try:
        return int(codigo[9:19])
    except ValueError:
        return None


def is_valor_confirmado_codigo_barras(codigo: str) -> bool:
    """Indica se o código de barras contém um valor monetário efetivo (> 0)."""
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return False
    if codigo[0] == "8":
        valor, efetivo = extrair_valor_arrecadacao(codigo)
        return efetivo and valor is not None and valor > 0
    valor = extrair_valor_codigo_barras(codigo)
    return valor is not None and valor > 0


def extrair_fator_vencimento(codigo: str) -> int | None:
    """Extrai o fator de vencimento (posições 5..8) de boleto bancário (44 dígitos)."""
    if len(codigo) != CODIGO_BARRAS_LENGTH or not codigo.isdigit():
        return None
    if codigo[0] == "8":
        return None

    try:
        return int(codigo[5:9])
    except ValueError:
        return None


def identificar_banco(codigo_ou_linha: str) -> tuple[str, str] | None:
    """
    Identifica o código e nome do banco/segmento emissor a partir do código ou linha.

    Returns:
        (codigo, nome) ou None
    """
    limpa = limpar_linha(codigo_ou_linha)
    if not limpa:
        return None
    if limpa.startswith("8") and len(limpa) in (CODIGO_BARRAS_LENGTH, ARRECADACAO_LENGTH):
        seg = limpa[1]
        return (f"8{seg}", f"Arrecadação ({SEGMENTOS_ARRECADACAO.get(seg, 'Concessionária')})")
    if len(limpa) >= 3:
        cod_banco = limpa[:3]
        nome = BANCOS_FEBRABAN.get(cod_banco)
        if nome:
            return (cod_banco, nome)
        return (cod_banco, f"Banco {cod_banco}")
    return None


def formatar_linha_digitavel(linha: str) -> str:
    """Formata linha digitável válida (47 ou 48 dígitos) com separadores padrão."""
    limpa = limpar_linha(linha)
    if not limpa:
        return linha
    if len(limpa) == BOLETO_LENGTH:
        return (
            f"{limpa[0:5]}.{limpa[5:10]} "
            f"{limpa[10:15]}.{limpa[15:21]} "
            f"{limpa[21:26]}.{limpa[26:32]} "
            f"{limpa[32]} {limpa[33:47]}"
        )
    if len(limpa) == ARRECADACAO_LENGTH:
        return (
            f"{limpa[0:11]}-{limpa[11]} "
            f"{limpa[12:23]}-{limpa[23]} "
            f"{limpa[24:35]}-{limpa[35]} "
            f"{limpa[36:47]}-{limpa[47]}"
        )
    return limpa
