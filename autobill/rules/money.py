"""Manipulação de valores monetários em centavos (inteiros)."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


class MoneyTypeError(TypeError):
    """Erro quando o tipo de centavos é inválido."""
    MSG = "centavos must be int"


@dataclass
class Money:
    """Valor monetário armazenado em centavos (inteiro)."""
    centavos: int

    def __post_init__(self) -> None:
        if not isinstance(self.centavos, int):
            raise MoneyTypeError(MoneyTypeError.MSG)

    def __hash__(self) -> int:
        return hash(self.centavos)

    @classmethod
    def from_reais(cls, reais: str | float | Decimal) -> "Money":
        """Cria Money a partir de valor em reais."""
        if isinstance(reais, str):
            # Remove formatação brasileira
            reais = reais.replace(".", "").replace(",", ".")
            valor = Decimal(reais)
        elif isinstance(reais, float):
            valor = Decimal(str(reais))
        else:
            valor = reais

        # Arredonda para 2 casas decimais e converte para centavos
        centavos = int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return cls(centavos)

    @classmethod
    def from_centavos(cls, centavos: int) -> "Money":
        """Cria Money a partir de centavos."""
        return cls(centavos)

    def to_reais(self) -> Decimal:
        """Converte para Decimal em reais."""
        return Decimal(self.centavos) / 100

    def to_reais_str(self) -> str:
        """Formata como string brasileira (R$ X,XX)."""
        reais = self.to_reais()
        # Formata com 2 casas decimais
        return f"R$ {reais:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.centavos + other.centavos)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.centavos - other.centavos)

    def __mul__(self, factor: int | float) -> "Money":
        if isinstance(factor, float):
            factor_decimal = Decimal(str(factor))
            result = Decimal(self.centavos) * factor_decimal
            return Money(int(result.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        return Money(self.centavos * factor)

    def __truediv__(self, divisor: int | float) -> "Money":
        divisor_decimal = Decimal(str(divisor)) if isinstance(divisor, float) else Decimal(divisor)
        result = Decimal(self.centavos) / divisor_decimal
        return Money(int(result.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return False
        return self.centavos == other.centavos

    def __lt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.centavos < other.centavos

    def __le__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.centavos <= other.centavos

    def __gt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.centavos > other.centavos

    def __ge__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.centavos >= other.centavos

    def __repr__(self) -> str:
        return f"Money(centavos={self.centavos})"

    def __str__(self) -> str:
        return self.to_reais_str()


def parse_valor_br(valor_str: str) -> int | None:
    """
    Parseia valor em formato brasileiro para centavos.

    Args:
        valor_str: String como "1.234,56" ou "1234,56" ou "1234.56"

    Returns:
        Valor em centavos ou None se inválido
    """
    if not valor_str:
        return None

    try:
        # Remove espaços e R$
        limpo = valor_str.strip().replace("R$", "").replace("r$", "").strip()

        # Detecta formato brasileiro (vírgula como decimal)
        if "," in limpo and "." in limpo:
            # Tem ambos: assume brasileiro (ponto = milhar, vírgula = decimal)
            limpo = limpo.replace(".", "").replace(",", ".")
        elif "," in limpo:
            # Só vírgula: assume decimal brasileiro
            limpo = limpo.replace(",", ".")
        # Se só ponto: assume decimal padrão

        valor = Decimal(limpo)
        return int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def formatar_centavos(centavos: int) -> str:
    """Formata centavos como string brasileira."""
    return Money(centavos).to_reais_str()


def comparar_valores(valor1: int, valor2: int, tolerancia: int = 1) -> bool:
    """
    Compara dois valores em centavos com tolerância.

    Args:
        valor1: Primeiro valor em centavos
        valor2: Segundo valor em centavos
        tolerancia: Tolerância em centavos (padrão 1 = R$ 0,01)

    Returns:
        True se diferença <= tolerância
    """
    return abs(valor1 - valor2) <= tolerancia
