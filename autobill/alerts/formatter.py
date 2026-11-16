"""Formatação de alertas proativos e resumos periódicos para Telegram."""

from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from autobill.alerts.policy import AlertDecision, check_anti_golpe_bank_mismatch
from autobill.db.models import Bill
from autobill.rules.boleto import formatar_linha_digitavel, identificar_banco
from autobill.rules.money import formatar_centavos


def format_proactive_alert(bill: Bill, decision: AlertDecision) -> str:
    """
    Formata alerta proativo em até 12 linhas (RNF15), com escape HTML (RF19)
    e informação essencial independente de emojis.
    """
    if decision.days_until < 0:
        header = f"🚨 <b>ATRASADA há {abs(decision.days_until)} dia(s)</b> (#{bill.id})"
    elif decision.days_until == 0:
        header = f"🚨 <b>VENCE HOJE</b> (#{bill.id})"
    elif decision.days_until == 1:
        header = f"⚠️ <b>Vence amanhã</b> (#{bill.id})"
    else:
        header = f"📅 <b>Vence em {decision.days_until} dias</b> (#{bill.id})"

    benef = escape(bill.beneficiario or "Beneficiário não informado")
    valor = formatar_centavos(bill.valor_centavos) if bill.valor_centavos is not None else "N/D"
    venc_str = bill.vencimento.strftime("%d/%m/%Y") if bill.vencimento else "Sem data"

    lines = [
        header,
        f"Beneficiário: <b>{benef}</b>",
        f"Valor: <b>{valor}</b> | Vencimento: <b>{venc_str}</b>",
    ]

    if decision.non_business_day_note:
        lines.append(f"Aviso: {escape(decision.non_business_day_note)}")

    codigo_ref = bill.linha_digitavel or bill.codigo_barras
    if codigo_ref:
        ident = identificar_banco(codigo_ref)
        if ident:
            lines.append(f"Emissor código: {escape(ident[1])} ({ident[0]})")
        mismatch = check_anti_golpe_bank_mismatch(bill.beneficiario, codigo_ref)
        if mismatch:
            lines.append(f"⚠️ <b>{escape(mismatch)}</b>")

    if bill.linha_digitavel:
        fmt_linha = escape(formatar_linha_digitavel(bill.linha_digitavel))
        lines.append(f"Linha digitável:\n<code>{fmt_linha}</code>")
    else:
        lines.append("Linha digitável: <i>código não disponível</i>")

    return "\n".join(lines[:12])


def get_alert_action_keyboard(bill: Bill) -> InlineKeyboardMarkup:
    """Botões inline obrigatórios do alerta (RF13, RF19)."""
    row1 = [
        InlineKeyboardButton("✅ Paguei", callback_data=f"pay:{bill.id}"),
        InlineKeyboardButton("⏰ Lembrar depois", callback_data=f"snooze_menu:{bill.id}"),
    ]
    file_label = "📄 Ver arquivo" if bill.linha_digitavel else "🔄 Reenviar / Ver arquivo"
    row2 = [InlineKeyboardButton(file_label, callback_data=f"send_file:{bill.id}")]
    return InlineKeyboardMarkup([row1, row2])


def format_weekly_summary(
    upcoming: list[Bill],
    overdue: list[Bill],
    paid_recent: list[Bill],
) -> str:
    """Resumo semanal enviado aos domingos às 18:00 (RF20)."""
    total_upcoming = sum(b.valor_centavos or 0 for b in upcoming)
    total_overdue = sum(b.valor_centavos or 0 for b in overdue)
    total_paid = sum(b.valor_centavos or 0 for b in paid_recent)

    lines = [
        "📊 <b>Resumo Semanal AutoBill</b>",
        "",
        f"⏳ <b>A vencer (próx. 7 dias):</b> {len(upcoming)} ({formatar_centavos(total_upcoming)})",
        f"⚠️ <b>Vencidas:</b> {len(overdue)} ({formatar_centavos(total_overdue)})",
        f"✅ <b>Pagas (últimos 7 dias):</b> {len(paid_recent)} ({formatar_centavos(total_paid)})",
    ]
    if overdue:
        lines.append("\n<b>Em atraso:</b>")
        for b in overdue[:5]:
            nome = escape((b.beneficiario or "Sem nome")[:28])
            val = formatar_centavos(b.valor_centavos or 0)
            dt = b.vencimento.strftime("%d/%m") if b.vencimento else "?"
            lines.append(f"• #{b.id} {nome} — {val} ({dt})")
    if upcoming:
        lines.append("\n<b>Próximos vencimentos:</b>")
        for b in upcoming[:5]:
            nome = escape((b.beneficiario or "Sem nome")[:28])
            val = formatar_centavos(b.valor_centavos or 0)
            dt = b.vencimento.strftime("%d/%m") if b.vencimento else "?"
            lines.append(f"• #{b.id} {nome} — {val} ({dt})")
    return "\n".join(lines)


def format_monthly_summary(year: int, month: int, summary: dict) -> str:
    """Fechamento mensal enviado no dia 1º de cada mês (RF20)."""
    return (
        f"📆 <b>Fechamento Mensal {month:02d}/{year}</b>\n\n"
        f"• Previsto: {summary.get('previsto', 0)} faturas "
        f"({formatar_centavos(int(summary.get('total_previsto', 0)))})\n"
        f"• Pago: {summary.get('pago', 0)} faturas "
        f"({formatar_centavos(int(summary.get('total_pago', 0)))})\n"
        f"• Pendente: {summary.get('pendente', 0)} faturas "
        f"({formatar_centavos(int(summary.get('total_pendente', 0)))})\n"
        f"• Vencida: {summary.get('vencida', 0)} faturas "
        f"({formatar_centavos(int(summary.get('total_vencida', 0)))})"
    )
