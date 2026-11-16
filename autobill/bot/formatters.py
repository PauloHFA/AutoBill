"""Message formatters for Telegram bot."""

from html import escape

from autobill.alerts.policy import check_anti_golpe_bank_mismatch, local_today
from autobill.config import settings
from autobill.db.models import Bill, BillStatus
from autobill.rules.boleto import formatar_linha_digitavel, identificar_banco
from autobill.rules.due_date import descrever_dia_nao_util
from autobill.rules.money import formatar_centavos

DUE_SOON_DAYS = 3
HIGH_CONFIDENCE_PERCENT = 80
MEDIUM_CONFIDENCE_PERCENT = 50


def format_bill_summary(bill: Bill, show_id: bool = True) -> str:
    """Format a bill as a summary line (HTML escaped)."""
    parts = []

    if show_id:
        parts.append(f"#{bill.id}")

    if bill.valor_centavos is not None:
        parts.append(f"💰 {formatar_centavos(bill.valor_centavos)}")

    if bill.vencimento:
        parts.append(f"📅 {bill.vencimento.strftime('%d/%m/%Y')}")

    if bill.beneficiario:
        parts.append(f"🏢 {escape(bill.beneficiario[:30])}")

    status_emoji = {
        BillStatus.EM_REVISAO: "✏️",
        BillStatus.PENDENTE: "⏳",
        BillStatus.PAGO: "✅",
        BillStatus.VENCIDA: "⚠️",
        BillStatus.DESCARTADA: "❌",
    }
    parts.append(f"{status_emoji.get(bill.status, '❓')} {bill.status.value}")

    return " | ".join(parts)


def format_bill_details(bill: Bill, show_confidence: bool = True) -> str:
    """Format full bill details for review and /fatura (HTML escaped)."""
    lines = [
        f"<b>📄 Boleto #{bill.id}</b>",
        f"<b>Status:</b> {bill.status.value}",
        "",
    ]

    if bill.valor_centavos is not None:
        conf_txt = "" if bill.valor_confirmado else " <i>(não confirmado no código)</i>"
        lines.append(f"💰 <b>Valor:</b> {formatar_centavos(bill.valor_centavos)}{conf_txt}")

    if bill.vencimento:
        venc = bill.vencimento
        today = local_today(settings.default_timezone)
        days_diff = (venc.date() - today).days

        if days_diff < 0:
            status = f"⚠️ VENCIDO há {abs(days_diff)} dia(s)"
        elif days_diff == 0:
            status = "⚠️ VENCE HOJE"
        elif days_diff <= DUE_SOON_DAYS:
            status = f"⚠️ Vence em {days_diff} dia(s)"
        else:
            status = f"📅 Em {days_diff} dia(s)"

        lines.append(f"📅 <b>Vencimento:</b> {venc.strftime('%d/%m/%Y')} ({status})")
        nao_util = descrever_dia_nao_util(venc.date())
        if nao_util:
            lines.append(f"🏦 <i>{escape(nao_util)}</i>")

    if bill.beneficiario:
        lines.append(f"🏢 <b>Beneficiário:</b> {escape(bill.beneficiario)}")

    codigo_ref = bill.linha_digitavel or bill.codigo_barras
    if codigo_ref:
        ident = identificar_banco(codigo_ref)
        if ident:
            lines.append(f"🏛️ <b>Emissor código:</b> {escape(ident[1])} ({ident[0]})")
        mismatch = check_anti_golpe_bank_mismatch(bill.beneficiario, codigo_ref)
        if mismatch:
            lines.append(f"⚠️ <b>{escape(mismatch)}</b>")

    if bill.linha_digitavel:
        fmt = escape(formatar_linha_digitavel(bill.linha_digitavel))
        lines.append(f"🔢 <b>Linha Digitável:</b>\n<code>{fmt}</code>")
    else:
        lines.append("🔢 <b>Linha Digitável:</b> <i>código não disponível</i>")

    if bill.codigo_barras:
        lines.append(f"📊 <b>Código de Barras:</b> <code>{escape(bill.codigo_barras)}</code>")

    lines.append(f"📥 <b>Origem:</b> {bill.origem.value}")

    if bill.paid_at:
        quem = f" por {escape(bill.paid_by)}" if bill.paid_by else ""
        lines.append(f"✅ <b>Pago em:</b> {bill.paid_at.strftime('%d/%m/%Y %H:%M')}{quem}")

    if bill.review_notes:
        lines.append(f"⚠️ <b>Observações:</b> {escape(bill.review_notes)}")

    if show_confidence and bill.confidence is not None:
        conf_pct = int(bill.confidence * 100)
        conf_emoji = (
            "🟢"
            if conf_pct >= HIGH_CONFIDENCE_PERCENT
            else "🟡"
            if conf_pct >= MEDIUM_CONFIDENCE_PERCENT
            else "🔴"
        )
        lines.append(f"{conf_emoji} <b>Confiança:</b> {conf_pct}%")

    return "\n".join(lines)


def format_bill_list(bills: list[Bill], page: int, total_pages: int, filter_name: str) -> str:
    """Format a list of bills."""
    if not bills:
        return f"📭 Nenhum boleto encontrado (filtro: {escape(filter_name)})"

    lines = [
        f"<b>📋 Boletos - {escape(filter_name.title())} (Página {page}/{max(1, total_pages)})</b>",
        "",
    ]

    lines.extend(format_bill_summary(bill) for bill in bills)

    return "\n".join(lines)


def format_alert_message(bill: Bill, alert_type: str = "upcoming") -> str:
    """Format alert message for a bill."""
    if alert_type == "upcoming":
        if bill.vencimento is None:
            urgency = "📅 <b>Vencimento próximo</b>"
        else:
            days = (bill.vencimento.date() - local_today(settings.default_timezone)).days
            if days == 0:
                urgency = "🚨 <b>VENCE HOJE!</b>"
            elif days == 1:
                urgency = "⚠️ <b>Vence amanhã!</b>"
            else:
                urgency = f"📅 <b>Vence em {days} dias</b>"

        return f"{urgency}\n\n{format_bill_details(bill, show_confidence=False)}"

    if alert_type == "overdue":
        if bill.vencimento is not None:
            days = (local_today(settings.default_timezone) - bill.vencimento.date()).days
            return (
                f"🚨 <b>BOLETO VENCIDO HÁ {days} DIAS!</b>\n\n"
                f"{format_bill_details(bill, show_confidence=False)}"
            )
        return f"🚨 <b>BOLETO VENCIDO!</b>\n\n{format_bill_details(bill, show_confidence=False)}"

    if alert_type == "snooze_end":
        return (
            f"⏰ <b>Lembrete de boleto adiado</b>\n\n"
            f"{format_bill_details(bill, show_confidence=False)}"
        )

    return format_bill_details(bill, show_confidence=False)


def format_help_message(category: str | None = None) -> str:
    """Format help message."""
    if category == "commands":
        return (
            "<b>📋 Comandos Disponíveis</b>\n\n"
            "/pendentes - Faturas pendentes e vencidas\n"
            "/proximos [dias] - Próximos vencimentos\n"
            "/mes [AAAA-MM] - Resumo financeiro do mês\n"
            "/fatura &lt;id&gt; - Detalhes e arquivo original\n"
            "/pagas - Faturas pagas\n"
            "/pago &lt;id&gt; - Marcar como paga\n"
            "/desfazer - Reverter último pagamento (24h)\n"
            "/adicionar - Lançamento manual passo a passo\n"
            "/editar &lt;id&gt; - Editar campos da fatura\n"
            "/remover &lt;id&gt; - Remover fatura (recuperável 30d)\n"
            "/review - Revisar faturas pendentes\n"
            "/paginas - Agrupar múltiplas fotos numa fatura\n"
            "/exportar - Exportar faturas em CSV\n"
            "/config - Preferências de alertas e extração\n"
            "/status - Saúde e diagnóstico do sistema\n"
            "/apagar_tudo - Apagar todos os dados (LGPD)\n\n"
            "💡 <b>Dica:</b> Envie PDFs ou fotos de boletos diretamente no chat!"
        )

    if category == "review":
        return (
            "<b>✏️ Como Revisar Boletos</b>\n\n"
            "1. Use /pending para ver boletos aguardando revisão\n"
            "2. Use /review para revisar o próximo\n"
            "3. Escolha Confirmar, Editar ou Descartar."
        )

    if category == "alerts":
        return (
            "<b>🔔 Sistema de Alertas</b>\n\n"
            "Alertas automáticos escalonados:\n"
            "• D-3, D-1, D0 (configurável em /config)\n"
            "• Em atraso: diário até 7 dias, depois semanal\n"
            "• Resumo semanal (domingo 18h) e mensal (dia 1)"
        )

    if category == "config":
        return (
            "<b>⚙️ Configurações</b>\n\n"
            "Use <code>/config hora HH:MM</code>\n"
            "Use <code>/config antecedencia 5,2,0</code>\n"
            "Use <code>/config timezone America/Sao_Paulo</code>\n"
            "Use <code>/config moeda BRL</code>\n"
            "Use <code>/config auto_confirm true|false</code>\n"
            "Use <code>/config extraction_mode auto|cloud|local</code>\n"
            "Use <code>/config cloud_consent true|false</code>"
        )

    return format_help_message("commands")


def format_config_status(config: dict) -> str:
    """Format current configuration status."""
    lines = [
        "<b>⚙️ Configurações Atuais</b>",
        "",
        f"🕐 <b>Horário Alertas:</b> {escape(str(config.get('alert_time', '07:30')))}",
        f"📅 <b>Dias Antecedência:</b> {escape(str(config.get('lead_days', '3,1,0')))}",
        f"🌍 <b>Fuso Horário:</b> {escape(str(config.get('timezone', 'America/Fortaleza')))}",
        f"💱 <b>Moeda:</b> {escape(str(config.get('currency', 'BRL')))}",
        f"⚡ <b>Confirmação Automática:</b> {'✅ Sim' if config.get('auto_confirm') else '❌ Não'}",
        f"🔧 <b>Modo Extração:</b> {escape(str(config.get('extraction_mode', 'auto')))}",
        f"🤖 <b>Modelo LLM:</b> {escape(str(config.get('llm_model', 'qwen2.5-vl')))}",
        f"☁️ <b>Consentimento Nuvem:</b> {'✅ Sim' if config.get('cloud_consent') else '❌ Não'}",
        "",
        "Use os botões abaixo ou <code>/config &lt;campo&gt; &lt;valor&gt;</code>.",
    ]
    return "\n".join(lines)


def format_welcome_message(user_name: str) -> str:
    """Format welcome message for new users."""
    return (
        f"👋 Olá, {escape(user_name)}!\n\n"
        f"Bem-vindo ao <b>AutoBill</b> - seu monitor inteligente de boletos.\n\n"
        f"📋 <b>O que eu faço:</b>\n"
        f"• Extraio dados de boletos (PDF ou foto)\n"
        f"• Valido matematicamente (módulo 10/11, fator vencimento)\n"
        f"• Armazeno localmente com segurança\n"
        f"• Envio alertas proativos antes e após o vencimento\n\n"
        f"Use /ajuda para ver todos os comandos."
    )


def format_error_message(error: str, user_friendly: bool = True) -> str:
    """Format error message (HTML escaped)."""
    safe_err = escape(error)
    if user_friendly:
        return (
            "❌ <b>Ops, algo deu errado!</b>\n\n"
            f"<i>{safe_err}</i>\n\n"
            "Tente novamente ou use /ajuda para ajuda."
        )
    return f"❌ <b>Erro:</b> <code>{safe_err}</code>"
