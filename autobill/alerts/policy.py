"""Regras puras de política de alertas, recorrência e anti-golpe."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from autobill.db.models import AlertType
from autobill.rules.boleto import BANCOS_FEBRABAN, identificar_banco
from autobill.rules.due_date import descrever_dia_nao_util

DAILY_OVERDUE_LIMIT_DAYS = 7
WEEKLY_OVERDUE_INTERVAL_DAYS = 7
RECURRING_ANTICIPATION_DAYS = 5


@dataclass(frozen=True)
class AlertDecision:
    """Decisão calculada para disparo de alerta de uma fatura."""

    alert_type: AlertType
    rule: str
    days_until: int
    non_business_day_note: str | None = None


def local_today(tz_name: str = "America/Fortaleza") -> date:
    """Retorna a data local atual no fuso horário IANA configurado (RN08)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Fortaleza")
    return datetime.now(tz).date()


def parse_lead_days(raw: str | list[int]) -> list[int]:
    """Converte string '3,1,0' ou lista em inteiros ordenados decrescentes."""
    if isinstance(raw, str):
        values = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    else:
        values = [int(x) for x in raw if int(x) >= 0]
    return sorted(set(values), reverse=True)


def decide_bill_alert(
    *,
    bill_due: date,
    today: date,
    lead_days: list[int],
    last_overdue_sent: date | None = None,
) -> AlertDecision | None:
    """
    Aplica a régua de alertas escalonados (RF17, RN09):
    - Antecedências configuráveis (ex: D-3, D-1, D-0)
    - Em atraso: diário até 7 dias; semanal após 7 dias
    - Fora da janela: None
    """
    days_until = (bill_due - today).days
    note = descrever_dia_nao_util(bill_due)

    if days_until >= 0:
        if days_until not in lead_days:
            return None
        if days_until == 0:
            atype = AlertType.D0
        elif days_until == 1:
            atype = AlertType.D1
        elif days_until == 3:
            atype = AlertType.D3
        else:
            atype = AlertType.CUSTOM
        return AlertDecision(
            alert_type=atype,
            rule=f"D-{days_until}",
            days_until=days_until,
            non_business_day_note=note,
        )

    # Em atraso (days_until < 0)
    overdue_days = abs(days_until)
    if last_overdue_sent == today:
        return None
    if overdue_days <= DAILY_OVERDUE_LIMIT_DAYS:
        return AlertDecision(
            alert_type=AlertType.ATRASO,
            rule="ATRASO",
            days_until=days_until,
            non_business_day_note=note,
        )

    # Após 7 dias em atraso: alerta semanal
    if (
        last_overdue_sent is None
        or (today - last_overdue_sent).days >= WEEKLY_OVERDUE_INTERVAL_DAYS
    ):
        return AlertDecision(
            alert_type=AlertType.ATRASO,
            rule="ATRASO",
            days_until=days_until,
            non_business_day_note=note,
        )
    return None


def check_anti_golpe_bank_mismatch(
    beneficiario: str | None, codigo_ou_linha: str | None
) -> str | None:
    """
    Alerta anti-golpe (Seção 9): destaca se o nome extraído menciona explicitamente
    um banco distinto do banco emissor codificado na linha/código de barras.
    """
    if not beneficiario or not codigo_ou_linha:
        return None
    ident = identificar_banco(codigo_ou_linha)
    if not ident:
        return None
    cod_emissor, nome_emissor = ident
    if cod_emissor.startswith("8"):
        return None

    ben_lower = beneficiario.lower()
    for cod, nome in BANCOS_FEBRABAN.items():
        if cod == cod_emissor:
            continue
        aliases = [nome.lower()]
        if "brasil" in nome.lower():
            aliases.append("banco do brasil")
        elif "itau" in nome.lower() or "itaú" in nome.lower():
            aliases.extend(["itaú", "itau"])
        elif "caixa" in nome.lower():
            aliases.append("caixa economica")
        if any(alias in ben_lower for alias in aliases):
            return (
                f"Atenção anti-golpe: beneficiário cita '{nome}', mas código pertence ao "
                f"banco {cod_emissor} ({nome_emissor})."
            )
    return None


def predict_next_recurring_due(last_seen: date, typical_day: int) -> date:
    """Calcula a próxima data prevista mensal com base em `last_seen` e `typical_day`."""
    year = last_seen.year + (1 if last_seen.month == 12 else 0)
    month = 1 if last_seen.month == 12 else last_seen.month + 1
    max_day = monthrange(year, month)[1]
    return date(year, month, min(typical_day, max_day))


def should_alert_missing_recurring(
    *,
    last_seen: date,
    typical_day: int,
    today: date,
    last_alerted_at: date | None,
) -> tuple[bool, date]:
    """Verifica se uma conta recorrente prevista (RF16) está a <= 5 dias e ainda não chegou."""
    predicted = predict_next_recurring_due(last_seen, typical_day)
    days_left = (predicted - today).days
    if days_left > RECURRING_ANTICIPATION_DAYS:
        return (False, predicted)
    if last_alerted_at and (today - last_alerted_at).days < 15:
        return (False, predicted)
    return (True, predicted)
